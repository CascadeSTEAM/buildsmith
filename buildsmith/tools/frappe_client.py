"""The only thing in this package that speaks HTTP to a Frappe instance.

Everything else emits files. This module exists so that the ability to reach a
Frappe instance lives in exactly one place, behind exactly one gate, rather than
being spread across whichever tool needed it (ADR-006, ADR-007).

**The guarantee is the target, not the transport.** Buildsmith used to be unable
to reach any Frappe instance because nothing here could open a socket. That was
an accident of implementation and it stopped being viable when the tool had to
run in a container talking to a sibling — so the guarantee moved to where it was
always actually enforced:

    A site name outside LOCAL_ONLY is refused. There is no override flag.

Three gates, in order, before any write:

1. **Site name** must be in `LOCAL_ONLY`. No flag, no environment variable, no
   argument relaxes this.
2. **Host shape** must be loopback, a `.localhost` name, or a bare hostname with
   no dots — a container service name on a private network. A public FQDN or a
   routable IP is refused, so pointing this at a real deployment fails before it
   authenticates rather than after.
3. **A worker must be draining the queue** (TRAP-009). Saving a Builder
   Component calls `queue_action`, which locks the document *and* enqueues a
   job; with nothing draining the queue the lock is never released and the next
   write fails with DocumentLockedError. Over `bench` this was sidestepped by
   setting `frappe.flags.in_migrate`, which is not reachable over HTTP — so the
   worker has to genuinely be there.

**Credentials are read, never resolved.** The token comes from the environment
or an argument and this module will not look one up. Resolving secrets is the
operations project's job (ADR-002) and giving Buildsmith a Vaultwarden client
would be the first step to giving it reach it should not have.

Token auth is required for writes rather than merely preferred: Frappe applies
CSRF protection to cookie-authenticated POSTs, so a session login cannot write
without also scraping a CSRF token out of the desk boot payload. Requiring
`Authorization: token <key>:<secret>` is both simpler and the credential shape
an operator can scope and revoke.

Environment:
    BUILDSMITH_FRAPPE_URL     base URL (default http://127.0.0.1:8000)
    BUILDSMITH_FRAPPE_SITE    site name  (default sandbox.localhost)
    BUILDSMITH_FRAPPE_TOKEN   "<api_key>:<api_secret>" — required for writes
"""

from __future__ import annotations

import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

#: The only sites this package will ever write to. A site name that is not one
#: of these is refused outright — there is deliberately no override, because an
#: override is how "local only" becomes "local by default".
LOCAL_ONLY = ("sandbox.localhost", "roundtrip.localhost")

DEFAULT_URL = "http://127.0.0.1:8000"

#: A worker whose last heartbeat is older than this is treated as gone. RQ
#: workers heartbeat well inside a minute; two minutes tolerates a slow host
#: without tolerating a worker that actually died.
HEARTBEAT_GRACE = timedelta(minutes=2)

__all__ = ["FrappeClient", "LOCAL_ONLY", "RefusedTarget", "from_env"]


class RefusedTarget(Exception):
    """The requested target failed a gate. Never catch this to retry."""


class FrappeError(Exception):
    """The instance rejected a request."""


def _refuse_non_local_site(site: str) -> None:
    if site not in LOCAL_ONLY:
        raise RefusedTarget(
            f"REFUSED: '{site}' is not a local dev site.\n"
            f"  This package writes only to {', '.join(LOCAL_ONLY)} — a disposable\n"
            "  local container. Applying to any real site is an action, and actions go\n"
            "  through the operations project (ADR-002). There is no override flag, on\n"
            "  purpose.\n"
            "  Emit payloads with `buildsmith build` and hand them over with\n"
            "  `buildsmith handoff`."
        )


def _refuse_public_host(base_url: str) -> str:
    """Reject anything that looks like it could be a real deployment.

    Frappe resolves a site from the Host header, so a mis-set base URL plus a
    valid token is the one way the site allow-list alone could be satisfied
    while the bytes land somewhere else. A host that is loopback, `.localhost`,
    or a single dotless label (a container service name on a private network)
    cannot be a public deployment; anything else is refused.
    """
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in ("http", "https"):
        raise RefusedTarget(f"REFUSED: {base_url!r} is not an http(s) URL.")
    host = parsed.hostname or ""

    if host in ("127.0.0.1", "::1", "localhost"):
        return base_url.rstrip("/")
    if host.endswith(".localhost"):
        return base_url.rstrip("/")
    if "." not in host and ":" not in host and host:
        # A bare label: a docker-compose service name on a private network.
        return base_url.rstrip("/")

    raise RefusedTarget(
        f"REFUSED: '{host}' is not a local dev host.\n"
        "  Allowed: loopback, a *.localhost name, or a container service name.\n"
        "  A routable host is refused before authenticating, because Frappe picks\n"
        "  its site from the Host header and a mis-set URL is the one way the site\n"
        "  allow-list could be satisfied while the writes land somewhere real."
    )


class FrappeClient:
    """A gated HTTP client for a *local dev* Frappe instance."""

    def __init__(self, base_url: str, *, site: str, token: str | None = None) -> None:
        # Gates first, before anything is stored, so a refused client cannot be
        # half-constructed and reused.
        _refuse_non_local_site(site)
        self.base_url = _refuse_public_host(base_url)
        self.site = site
        self.token = token
        self._worker_checked = False

    # --- transport ---------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        raw_body: bytes | None = None,
        content_type: str | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v if isinstance(v, str) else json.dumps(v) for k, v in params.items()}
            )

        data = raw_body
        headers = {
            # Frappe resolves multi-tenant sites from Host. This is what makes
            # the site allow-list meaningful over a network transport.
            "Host": self.site,
            "Accept": "application/json",
        }
        if self.token:
            headers["Authorization"] = f"token {self.token}"
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        elif content_type:
            headers["Content-Type"] = content_type

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            raise FrappeError(self._explain(exc)) from None
        except urllib.error.URLError as exc:
            raise FrappeError(
                f"cannot reach {self.base_url} ({exc.reason}).\n"
                "  Is the dev instance running? `buildsmith sandbox status`"
            ) from None

        if not payload:
            return None
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return payload.decode(errors="replace")

    @staticmethod
    def _explain(exc: urllib.error.HTTPError) -> str:
        """Turn Frappe's error envelope into something a human can act on."""
        raw = exc.read().decode(errors="replace")
        detail = raw
        try:
            parsed = json.loads(raw)
            messages = parsed.get("_server_messages")
            if messages:
                detail = "; ".join(
                    json.loads(m).get("message", m) for m in json.loads(messages)
                )
            elif parsed.get("exception"):
                detail = parsed["exception"]
            elif parsed.get("exc_type"):
                detail = parsed["exc_type"]
        except (json.JSONDecodeError, TypeError, KeyError):
            pass

        if exc.code in (401, 403):
            detail += (
                "\n  Writes need token auth: set BUILDSMITH_FRAPPE_TOKEN to"
                " '<api_key>:<api_secret>'.\n"
                "  Buildsmith reads the token; it never resolves one (ADR-002)."
            )
        return f"HTTP {exc.code}: {detail}"

    # --- gate 3: a worker must be draining the queue -----------------------

    def require_worker(self) -> None:
        """TRAP-009, made structural.

        Saving a Builder Component calls `queue_action`, which locks the doc and
        enqueues a job. Over `bench` this was skipped by setting
        `frappe.flags.in_migrate`; that flag is unreachable over HTTP, so the
        job has to actually run or the lock is permanent.
        """
        if self._worker_checked:
            return

        try:
            path = f"/api/resource/{urllib.parse.quote('RQ Worker')}"
            workers = (self._request("GET", path) or {}).get("data", [])
        except FrappeError as exc:
            raise FrappeError(
                f"could not check for a running worker: {exc}\n"
                "  Refusing to write. A component save locks the document and enqueues a\n"
                "  job; with nothing draining the queue that lock is never released\n"
                "  (TRAP-009), and the failure surfaces later as a permission-looking\n"
                "  error on an unrelated write."
            ) from None

        alive = [w for w in workers if self._heartbeat_fresh(w.get("last_heartbeat"))]
        if not alive:
            raise FrappeError(
                "REFUSING TO WRITE: no worker is draining the queue.\n"
                "  Saving a Builder Component enqueues a job *and* locks the document\n"
                "  (TRAP-009). With no worker the lock is never released and the next\n"
                "  write fails with DocumentLockedError — which reads like a permissions\n"
                "  problem and is not one.\n"
                "  Start one: `buildsmith sandbox up` brings up the worker."
            )
        self._worker_checked = True

    @staticmethod
    def _heartbeat_fresh(stamp: str | None) -> bool:
        if not stamp:
            return False
        try:
            beat = datetime.fromisoformat(stamp)
        except ValueError:
            return False
        if beat.tzinfo is None:
            beat = beat.replace(tzinfo=UTC)
        return datetime.now(UTC) - beat < HEARTBEAT_GRACE

    # --- documents ---------------------------------------------------------

    def ping(self) -> bool:
        return self._request("GET", "/api/method/ping") == {"message": "pong"}

    def get_list(
        self, doctype: str, *, filters: Any = None, fields: list[str] | None = None,
        limit: int = 0,
    ) -> list[dict]:
        params: dict[str, Any] = {"fields": fields or ["name"]}
        if filters:
            params["filters"] = filters
        # Frappe pages at 20 by default, which silently truncates a clone.
        params["limit_page_length"] = limit or 0
        path = f"/api/resource/{urllib.parse.quote(doctype)}"
        return (self._request("GET", path, params=params) or {}).get("data", [])

    def get(self, doctype: str, name: str) -> dict:
        path = f"/api/resource/{urllib.parse.quote(doctype)}/{urllib.parse.quote(name, safe='')}"
        return (self._request("GET", path) or {}).get("data", {})

    def exists(self, doctype: str, filters: Any) -> str | None:
        found = self.get_list(doctype, filters=filters, fields=["name"], limit=1)
        return found[0]["name"] if found else None

    def insert(self, doctype: str, doc: dict) -> dict:
        self.require_worker()
        path = f"/api/resource/{urllib.parse.quote(doctype)}"
        return (self._request("POST", path, body={**doc, "doctype": doctype}) or {}).get("data", {})

    def update(self, doctype: str, name: str, doc: dict) -> dict:
        self.require_worker()
        path = f"/api/resource/{urllib.parse.quote(doctype)}/{urllib.parse.quote(name, safe='')}"
        return (self._request("PUT", path, body=doc) or {}).get("data", {})

    def delete(self, doctype: str, name: str) -> None:
        self.require_worker()
        path = f"/api/resource/{urllib.parse.quote(doctype)}/{urllib.parse.quote(name, safe='')}"
        self._request("DELETE", path)

    def set_single(self, doctype: str, field: str, value: Any) -> None:
        """Set one field on a Single doctype, e.g. Website Settings.home_page."""
        self.update(doctype, doctype, {field: value})

    # --- files -------------------------------------------------------------

    def upload_file(self, path: Path, *, private: bool = False) -> str:
        """Upload one asset and return its file_url.

        A clone whose images 404 is a wireframe, not a clone — so this is part
        of the load, not an optional extra.
        """
        self.require_worker()
        boundary = f"----buildsmith{uuid.uuid4().hex}"
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

        parts: list[bytes] = []
        for key, value in (("is_private", "1" if private else "0"), ("file_name", path.name)):
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n"
                f"{value}\r\n".encode()
            )
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{path.name}\"\r\nContent-Type: {content_type}\r\n\r\n".encode()
        )
        parts.append(path.read_bytes())
        parts.append(f"\r\n--{boundary}--\r\n".encode())

        result = self._request(
            "POST",
            "/api/method/upload_file",
            raw_body=b"".join(parts),
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        return (result or {}).get("message", {}).get("file_url", "")


def from_env(*, site: str | None = None, url: str | None = None) -> FrappeClient:
    """Build a client from the environment.

    The token is *read*, never resolved. Buildsmith has no secret store client
    and must not grow one — that is the operations project's job (ADR-002).
    """
    return FrappeClient(
        url or os.environ.get("BUILDSMITH_FRAPPE_URL") or DEFAULT_URL,
        site=site or os.environ.get("BUILDSMITH_FRAPPE_SITE") or LOCAL_ONLY[0],
        token=os.environ.get("BUILDSMITH_FRAPPE_TOKEN"),
    )
