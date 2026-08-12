"""Prove the sandbox reproduces known traps — the faithfulness check.

A sandbox that merely *runs* proves nothing. This proves it fails the way
production fails, on the traps whose failure modes are entirely silent.

The script that does the checking runs **inside** the bench container, because
it imports Builder. It lives beside this file as data rather than being embedded
as a string: a script-in-a-string needs escaping, and escaping is where a check
quietly stops checking what you think it checks.

**TRAP-019 is different in kind**, not just degree: its failure mode is in the
Builder *editor's* client-side render tree, which no bench-console script can
see — proving it needs a real browser against a real page, the same way the
trap itself was first found. `_check_editor_renders_map()` builds a throwaway
component and page (never touching anything a real site would have named),
drives a browser at the sandbox's own exposed port, and cleans up after itself
whether it passed or not. Playwright is optional (`pip install '.[visual]'`);
its absence is "could not check" (exit 2), never a silent skip — the same rule
`buildsmith verify` follows for the same reason.
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

from buildsmith.errors import CouldNotCheck
from buildsmith.primitives.components import compose, override_shells
from buildsmith.primitives.maps import location_map
from buildsmith.primitives.tokens import Applied
from buildsmith.tools.sandbox import load_pins, require_running, run_bench
from buildsmith.workflows.theme.build import resolve_tokens

SCRIPT = Path(__file__).parent / "bench_scripts" / "trap_check.py"

BASE_URL = "http://127.0.0.1:8000"
CHECK_COMPONENT_ID = "check-trap-019-map"
CHECK_ROUTE = "check-trap-019"

#: The only sites this check will ever write to. Mirrors `load_dev.py`'s
#: `LOCAL_ONLY` guard (ADR-002) for the same class of write, with no override
#: — this is the first mutation-capable code path `check_traps.py` has grown,
#: and a config mistake handing it a real site name must fail loudly, not
#: quietly insert a throwaway component into someone's actual data.
LOCAL_ONLY = ("sandbox.localhost", "roundtrip.localhost")

__all__ = ["main"]

#: Deliberately literal, not `@map-*` sigils: this check proves the *sandbox*
#: renders a correctly-shaped component, not our own token-resolution code
#: (tests/test_maps.py already covers that). An unresolved "@map-width" is
#: not valid CSS, and a box with invalid CSS looks broken for a reason that
#: has nothing to do with TRAP-019.
_APPLIED = Applied.from_dict(
    {
        "tokens": {
            "map-width": {"uuid": "u-w", "value": "100%"},
            "map-height": {"uuid": "u-h", "value": "320px"},
            "map-radius": {"uuid": "u-r", "value": "12px"},
            "map-border": {"uuid": "u-b", "value": "#d8ded9"},
        }
    }
)

# The page name is a hash Builder assigns (TRAP-012) — printed on a sentinel
# line so the host can pull it out of run_bench()'s stdout without guessing.
_SETUP = r"""
import json, frappe
frappe.init(site=%(site)r); frappe.connect()
frappe.flags.in_migrate = True

for name in frappe.get_all("Builder Page", filters={"route": %(route)r}, pluck="name"):
    frappe.delete_doc("Builder Page", name, force=True)
if frappe.db.exists("Builder Component", %(component_id)r):
    frappe.delete_doc("Builder Component", %(component_id)r, force=True)

frappe.get_doc({
    "doctype": "Builder Component",
    "component_id": %(component_id)r,
    "component_name": %(component_id)r,
    "block": json.loads(%(component_json)r),
}).insert()

page = frappe.get_doc({
    "doctype": "Builder Page",
    "title": %(component_id)r,
    "route": %(route)r,
    "blocks": json.dumps([json.loads(%(shell_json)r)]),
    "published": 1,
}).insert()
frappe.db.commit()
print("PAGE_NAME=" + page.name)
"""

_TEARDOWN = r"""
import frappe
frappe.init(site=%(site)r); frappe.connect()
frappe.flags.in_migrate = True

for name in frappe.get_all("Builder Page", filters={"route": %(route)r}, pluck="name"):
    frappe.delete_doc("Builder Page", name, force=True)
if frappe.db.exists("Builder Component", %(component_id)r):
    frappe.delete_doc("Builder Component", %(component_id)r, force=True)
frappe.db.commit()
print("torn down")
"""


def _check_bench_traps() -> tuple[bool, str]:
    out = run_bench(SCRIPT.read_text())
    return "NOT FAITHFUL" not in out, out


def _server_reachable(*, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=timeout):
            return True
    except OSError:
        return False


def _teardown(site: str) -> None:
    try:
        run_bench(_TEARDOWN % {"site": site, "route": CHECK_ROUTE,
                                "component_id": CHECK_COMPONENT_ID})
    except Exception as exc:  # noqa: BLE001 — best-effort cleanup, never masks the result
        print(f"  (cleanup warning: {exc})", file=sys.stderr)


def _check_editor_renders_map(site: str) -> bool:
    """TRAP-019: extend the map component onto a page and open the editor.

    Builds and tears down a component/page named unmistakably as a check
    fixture — never `location-map`, never a route a real site would use — so
    this can run against a sandbox someone is also using by hand without
    colliding with anything real.
    """
    if site not in LOCAL_ONLY:
        raise CouldNotCheck(
            f"refusing to write a check fixture into {site!r} — this check only ever runs "
            f"against {', '.join(LOCAL_ONLY)} (ADR-002). Check sandbox/pins.env's SITE_NAME."
        )

    if not _server_reachable():
        raise CouldNotCheck(
            f"nothing is answering on {BASE_URL} — the bench container is up but its web "
            "server is not. Run `buildsmith sandbox serve` first."
        )

    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        if "playwright" not in str(exc):
            raise
        raise CouldNotCheck(
            "playwright is not installed here. Install: pip install '.[visual]' && "
            "playwright install chromium"
        ) from exc

    component = compose(
        component_id=CHECK_COMPONENT_ID,
        component_name=CHECK_COMPONENT_ID,
        root=resolve_tokens(location_map(address="TRAP-019 check", lat=0.0, lon=0.0), _APPLIED),
        applied=_APPLIED,
    )
    instance_root = json.loads(json.dumps(component.block))  # a same-shape copy to shell against
    shell = override_shells(component.block, instance_root, component_id=CHECK_COMPONENT_ID)

    # From here on, a check fixture may exist in the sandbox regardless of how
    # this function exits — `_teardown` runs in `finally` starting now, not
    # after page-name extraction. A `SandboxError` from `run_bench` (a bad
    # docker-exec, not a scripted refusal) would otherwise skip cleanup and
    # escape `main()`'s narrower `except CouldNotCheck` as a raw traceback.
    try:
        out = run_bench(
            _SETUP % {
                "site": site,
                "route": CHECK_ROUTE,
                "component_id": CHECK_COMPONENT_ID,
                "component_json": json.dumps(component.block),
                "shell_json": json.dumps(shell),
            }
        )
        page_name = next(
            (line.split("=", 1)[1] for line in out.splitlines() if line.startswith("PAGE_NAME=")),
            None,
        )
        if not page_name:
            raise CouldNotCheck(f"setup did not report a page name — bench said:\n{out}")

        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch()
            except PlaywrightError as exc:
                raise CouldNotCheck(
                    f"chromium would not launch ({exc}). If it was never installed: "
                    "playwright install chromium"
                ) from exc
            context = browser.new_context(base_url=BASE_URL)
            pins = load_pins()
            login = context.request.post(
                f"{BASE_URL}/api/method/login",
                form={"usr": "Administrator", "pwd": pins.get("ADMIN_PASSWORD", "admin")},
            )
            if not login.ok:
                browser.close()
                raise CouldNotCheck(
                    f"login to {BASE_URL} failed ({login.status}) — a page rendering with no "
                    "iframe would otherwise be indistinguishable from a real TRAP-019 "
                    "regression."
                )
            page = context.new_page()
            page.goto(f"{BASE_URL}/builder/page/{page_name}", wait_until="networkidle")
            try:
                page.wait_for_selector('iframe[src*="openstreetmap.org"]', timeout=15000)
                rendered = True
            except PlaywrightTimeoutError:
                rendered = False
            browser.close()
    finally:
        _teardown(site)

    return rendered


def main(argv: list[str] | None = None) -> int:
    require_running()
    pins = load_pins()
    print(f"Builder pin: {pins.builder} ({pins.get('BUILDER_REF_STATUS')})\n")

    try:
        bench_ok, out = _check_bench_traps()
    except Exception as exc:  # noqa: BLE001 - the bench reports its own detail
        print(exc, file=sys.stderr)
        return 1
    sys.stdout.write(out)

    print("\nTRAP-019 — a component-extended iframe renders live in the editor\n")
    try:
        editor_ok = _check_editor_renders_map(pins.get("SITE_NAME", "sandbox.localhost"))
    except CouldNotCheck as exc:
        print(f"  ????  COULD NOT CHECK — {exc}")
        # "Could not check" must never read as "this is fine" — but a problem
        # the bench check already found must keep exit 1: not being able to
        # check *this* trap never outranks having found a different one.
        return 1 if not bench_ok else 2
    except Exception as exc:  # noqa: BLE001 - an unexpected failure is still "found a problem"
        print(f"  ????  COULD NOT CHECK — unexpected error: {exc}", file=sys.stderr)
        return 1

    print(f"  {'PASS' if editor_ok else 'FAIL'}  the map's iframe is in the editor's DOM")
    if not editor_ok:
        print(
            "  the component-extended iframe never reached the editor canvas. Either "
            "TRAP-019's fix (root the component on the iframe, not a wrapping div) has "
            "regressed, or the pinned Builder's editor render path has changed."
        )

    return 1 if not (bench_ok and editor_ok) else 0


if __name__ == "__main__":
    raise SystemExit(main())
