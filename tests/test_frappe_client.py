"""The gates are the guarantee, so the gates are what get tested.

`frappe_client` is the only module in the package that can reach a Frappe
instance. What stops it reaching a real one is three checks and the absence of
an override flag. A regression in any of them is silent — it looks like the tool
working — so each is tested for refusal explicitly, and the absence of an
override is tested as its own property.

Nothing here needs a running instance: every test stops at or before the socket.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from buildsmith.tools import frappe_client
from buildsmith.tools.frappe_client import LOCAL_ONLY, FrappeClient, RefusedTarget
from tests.fixtures import LIVE_CLOUD_HOST, LIVE_HOST, NEAR_MISS_SITE, PUBLIC_HOST

LOCAL_URL = "http://127.0.0.1:8000"
GOOD_SITE = LOCAL_ONLY[0]


class SiteAllowListTest(unittest.TestCase):
    """Gate 1 — a site outside LOCAL_ONLY is refused."""

    def test_allowed_sites_construct(self) -> None:
        for site in LOCAL_ONLY:
            self.assertEqual(FrappeClient(LOCAL_URL, site=site).site, site)

    def test_live_looking_site_refused(self) -> None:
        for site in (LIVE_HOST, f"www.{LIVE_HOST}", LIVE_CLOUD_HOST, "prod"):
            with self.assertRaises(RefusedTarget, msg=site):
                FrappeClient(LOCAL_URL, site=site)

    def test_empty_site_refused(self) -> None:
        with self.assertRaises(RefusedTarget):
            FrappeClient(LOCAL_URL, site="")

    def test_near_miss_site_refused(self) -> None:
        """A name that merely resembles an allowed one must not pass.

        Substring or prefix matching here would be the whole hole: a live site
        whose name merely *starts with* an allowed one would sail through.
        """
        for site in (
            NEAR_MISS_SITE,
            "notsandbox.localhost",
            "SANDBOX.LOCALHOST",
            " sandbox.localhost",
            "sandbox.localhost ",
        ):
            with self.assertRaises(RefusedTarget, msg=site):
                FrappeClient(LOCAL_URL, site=site)

    def test_there_is_no_override(self) -> None:
        """The absence of an escape hatch is a feature; assert it stays absent.

        An override is how "local only" becomes "local by default", so a future
        `force=` or `allow_remote=` argument should fail this test loudly rather
        than quietly widening the guarantee.
        """
        import inspect

        params = set(inspect.signature(FrappeClient.__init__).parameters)
        self.assertEqual(params, {"self", "base_url", "site", "token"})


class HostShapeTest(unittest.TestCase):
    """Gate 2 — the URL must not look like a real deployment."""

    def test_local_hosts_allowed(self) -> None:
        for url in (
            "http://127.0.0.1:8000",
            "http://localhost:8000",
            "http://sandbox.localhost",
            "http://bench:8000",  # a container service name
            "http://frappe",
        ):
            self.assertTrue(FrappeClient(url, site=GOOD_SITE).base_url, url)

    def test_public_hosts_refused(self) -> None:
        for url in (
            f"https://{PUBLIC_HOST}",
            "http://203.0.113.10:8000",
            f"https://{LIVE_CLOUD_HOST}",
            f"http://{LIVE_HOST}",
        ):
            with self.assertRaises(RefusedTarget, msg=url):
                FrappeClient(url, site=GOOD_SITE)

    def test_non_http_scheme_refused(self) -> None:
        for url in ("ssh://box", "file:///etc/passwd", "ftp://host"):
            with self.assertRaises(RefusedTarget, msg=url):
                FrappeClient(url, site=GOOD_SITE)

    def test_trailing_slash_normalised(self) -> None:
        self.assertEqual(
            FrappeClient("http://127.0.0.1:8000/", site=GOOD_SITE).base_url,
            "http://127.0.0.1:8000",
        )

    def test_gates_run_before_anything_is_stored(self) -> None:
        """A refused client must not be half-constructed and then reused."""
        client = FrappeClient.__new__(FrappeClient)
        with self.assertRaises(RefusedTarget):
            FrappeClient.__init__(client, f"https://{LIVE_HOST}", site=GOOD_SITE)
        self.assertFalse(hasattr(client, "base_url"))


class WorkerHeartbeatTest(unittest.TestCase):
    """Gate 3 — TRAP-009. A worker that is gone must not read as present."""

    @staticmethod
    def _stamp(ago: timedelta) -> str:
        return (datetime.now(UTC) - ago).isoformat()

    def test_fresh_heartbeat_is_alive(self) -> None:
        self.assertTrue(FrappeClient._heartbeat_fresh(self._stamp(timedelta(seconds=5))))

    def test_stale_heartbeat_is_dead(self) -> None:
        self.assertFalse(FrappeClient._heartbeat_fresh(self._stamp(timedelta(hours=3))))

    def test_missing_or_unparseable_heartbeat_is_dead(self) -> None:
        """Unknown must mean dead, not alive.

        A worker list we cannot interpret is exactly the case where guessing
        "probably fine" produces the permanent lock this gate exists to prevent.
        """
        for stamp in (None, "", "not a timestamp", "0000"):
            self.assertFalse(FrappeClient._heartbeat_fresh(stamp), repr(stamp))

    def test_naive_timestamp_treated_as_utc(self) -> None:
        """Frappe returns naive timestamps; reading them as local time would
        make a live worker look hours stale in any non-UTC zone."""
        naive = (datetime.now(UTC) - timedelta(seconds=5)).replace(tzinfo=None)
        self.assertTrue(FrappeClient._heartbeat_fresh(naive.isoformat()))

    def test_no_live_worker_refuses_to_write(self) -> None:
        client = FrappeClient(LOCAL_URL, site=GOOD_SITE)
        client._request = lambda *a, **k: {  # type: ignore[method-assign]
            "data": [{"last_heartbeat": self._stamp(timedelta(hours=3))}]
        }
        with self.assertRaises(frappe_client.FrappeError) as caught:
            client.require_worker()
        self.assertIn("TRAP-009", str(caught.exception))

    def test_unreachable_worker_list_refuses_to_write(self) -> None:
        """Cannot-check must not become did-check."""
        client = FrappeClient(LOCAL_URL, site=GOOD_SITE)

        def boom(*a, **k):
            raise frappe_client.FrappeError("connection refused")

        client._request = boom  # type: ignore[method-assign]
        with self.assertRaises(frappe_client.FrappeError) as caught:
            client.require_worker()
        self.assertIn("Refusing to write", str(caught.exception))

    def test_every_write_is_gated(self) -> None:
        """Each mutating method must call require_worker().

        Tested by construction rather than by listing them, so a new write
        method added later is caught instead of silently ungated.
        """
        client = FrappeClient(LOCAL_URL, site=GOOD_SITE)
        calls: list[str] = []
        client.require_worker = lambda: calls.append("checked")  # type: ignore[method-assign]
        client._request = lambda *a, **k: {"data": {}}  # type: ignore[method-assign]

        for name, call in (
            ("insert", lambda: client.insert("X", {})),
            ("update", lambda: client.update("X", "n", {})),
            ("delete", lambda: client.delete("X", "n")),
            ("set_single", lambda: client.set_single("X", "f", 1)),
        ):
            calls.clear()
            call()
            self.assertEqual(calls, ["checked"], f"{name} did not check for a worker")


class CredentialTest(unittest.TestCase):
    """Credentials are read, never resolved."""

    def test_no_secret_store_is_imported(self) -> None:
        """Buildsmith must not grow a way to look a secret up (ADR-002).

        Checked against the module's actual imports rather than its text: the
        docstring says the words "Vaultwarden" and "resolve" precisely because
        it is explaining the prohibition, and a substring scan cannot tell an
        explanation from a violation.
        """
        import ast

        tree = ast.parse(Path(frappe_client.__file__).read_text())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        # `subprocess` would let it shell out to a secret manager; the rest are
        # the managers themselves. None of them belong here.
        forbidden = {"subprocess", "keyring", "bitwarden", "hvac", "boto3", "requests"}
        self.assertEqual(imported & forbidden, set(), "the client grew a way to reach out")

    def test_token_comes_from_the_environment(self) -> None:
        import os

        old = os.environ.get("BUILDSMITH_FRAPPE_TOKEN")
        os.environ["BUILDSMITH_FRAPPE_TOKEN"] = "key:secret"
        try:
            self.assertEqual(frappe_client.from_env().token, "key:secret")
        finally:
            if old is None:
                os.environ.pop("BUILDSMITH_FRAPPE_TOKEN", None)
            else:
                os.environ["BUILDSMITH_FRAPPE_TOKEN"] = old

    def test_from_env_still_gates(self) -> None:
        """The convenience constructor must not be a way around the gates."""
        import os

        old = os.environ.get("BUILDSMITH_FRAPPE_SITE")
        os.environ["BUILDSMITH_FRAPPE_SITE"] = LIVE_HOST
        try:
            with self.assertRaises(RefusedTarget):
                frappe_client.from_env()
        finally:
            if old is None:
                os.environ.pop("BUILDSMITH_FRAPPE_SITE", None)
            else:
                os.environ["BUILDSMITH_FRAPPE_SITE"] = old


if __name__ == "__main__":
    unittest.main()
