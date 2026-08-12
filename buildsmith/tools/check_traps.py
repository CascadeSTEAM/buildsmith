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
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from buildsmith.errors import CouldNotCheck
from buildsmith.primitives.blocks import new_block
from buildsmith.primitives.components import compose, override_shells
from buildsmith.primitives.datapage import list_data_script
from buildsmith.primitives.maps import location_map
from buildsmith.primitives.repeater import binding, repeater
from buildsmith.primitives.tokens import Applied
from buildsmith.tools.sandbox import load_pins, require_running, run_bench
from buildsmith.workflows.theme.build import resolve_tokens

SCRIPT = Path(__file__).parent / "bench_scripts" / "trap_check.py"

# Matches sandbox.py's own COMPOSE/BENCH — each tool that shells out to the
# container defines these locally rather than importing a private helper.
_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE = ["docker", "compose", "-f", str(_ROOT / "sandbox" / "docker-compose.yml")]
_BENCH = "/home/frappe/frappe-bench"

BASE_URL = "http://127.0.0.1:8000"
CHECK_COMPONENT_ID = "check-trap-019-map"
CHECK_ROUTE = "check-trap-019"
CHECK_DOCTYPE = "Check Trap 020 Item"
CHECK_020_ROUTE = "check-trap-020"

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


# TRAP-020: a throwaway custom doctype with only System Manager permission —
# the default a `bench` DocType creation leaves you at, and deliberately
# never given Guest read. The whole point is to prove `frappe.db.get_all`
# does not need it and `frappe.db.get_list` does.
_DOCTYPE_SETUP = r"""
import frappe
from frappe.utils.safe_exec import is_safe_exec_enabled
frappe.init(site=%(site)r); frappe.connect()
frappe.flags.in_migrate = True

# TRAP-020's whole finding is conditional on this being False (Builder's own
# safer_exec, not a Server Script's safe_exec) — state plainly what this run
# actually checked rather than silently assuming the pin never changed it.
print("SAFE_EXEC_ENABLED=" + str(bool(is_safe_exec_enabled())))

if frappe.db.exists("DocType", %(doctype)r):
    frappe.delete_doc("DocType", %(doctype)r, force=True, ignore_permissions=True)
# Deleting a DocType record does not reliably drop its table (observed at
# this pin: the meta record was gone, the table and its rows were not) — an
# explicit drop is what actually makes this setup idempotent across runs.
frappe.db.sql_ddl(%(drop_sql)r)

frappe.get_doc({
    "doctype": "DocType",
    "name": %(doctype)r,
    "module": "Builder",
    "custom": 1,
    "naming_rule": "Set by user",
    "autoname": "field:item_name",
    "fields": [
        {"fieldname": "item_name", "fieldtype": "Data", "label": "Item Name",
         "reqd": 1, "unique": 1},
        {"fieldname": "price", "fieldtype": "Data", "label": "Price"},
    ],
    "permissions": [
        {"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
    ],
}).insert(ignore_permissions=True)

for item_name, price in %(records)r:
    frappe.get_doc({
        "doctype": %(doctype)r, "item_name": item_name, "price": price,
    }).insert(ignore_permissions=True)
frappe.db.commit()
print("doctype and records ready")
"""

_PAGE_020_SETUP = r"""
import frappe
frappe.init(site=%(site)r); frappe.connect()
frappe.flags.in_migrate = True

for name in frappe.get_all("Builder Page", filters={"route": %(route)r}, pluck="name"):
    frappe.delete_doc("Builder Page", name, force=True)

page = frappe.get_doc({
    "doctype": "Builder Page",
    "title": %(route)r,
    "route": %(route)r,
    "blocks": %(blocks_json)r,
    "published": 1,
    "page_data_script": %(script)r,
}).insert()
frappe.db.commit()
print("PAGE_NAME=" + page.name)
"""

_020_TEARDOWN = r"""
import frappe
frappe.init(site=%(site)r); frappe.connect()
frappe.flags.in_migrate = True

for route in %(routes)r:
    for name in frappe.get_all("Builder Page", filters={"route": route}, pluck="name"):
        frappe.delete_doc("Builder Page", name, force=True)
if frappe.db.exists("DocType", %(doctype)r):
    frappe.delete_doc("DocType", %(doctype)r, force=True, ignore_permissions=True)
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


def _clear_caches(site: str) -> None:
    """TRAP-015: a page recreated at an already-used route 403s until both
    the route cache and the rendered-page cache are cleared — `on_update`
    does not reliably do this on its own (observed at this pin: a page
    inserted fresh at a route a previous run also used 403'd for an
    anonymous visitor until this ran). Same two commands `load_dev.py` runs
    after every load, for the same reason.

    A silent failure here would surface downstream as a false TRAP-020
    regression — "the page 403'd" is exactly this bug's own symptom — so a
    non-zero exit is reported loudly rather than swallowed.
    """
    completed = subprocess.run(
        [*_COMPOSE, "exec", "-T", "bench", "bash", "-lc",
         f"cd {_BENCH} && bench --site {site} clear-website-cache && "
         f"bench --site {site} clear-cache"],
        capture_output=True, text=True, timeout=60,
    )
    if completed.returncode != 0:
        print(
            f"  (cache clear failed, exit {completed.returncode}: "
            f"{completed.stderr.strip()[-300:]})",
            file=sys.stderr,
        )


def _teardown(site: str) -> None:
    try:
        run_bench(_TEARDOWN % {"site": site, "route": CHECK_ROUTE,
                                "component_id": CHECK_COMPONENT_ID})
    except Exception as exc:  # noqa: BLE001 — best-effort cleanup, never masks the result
        print(f"  (cleanup warning: {exc})", file=sys.stderr)


def _teardown_020(site: str, *, routes: tuple[str, ...]) -> None:
    try:
        run_bench(_020_TEARDOWN % {"site": site, "routes": routes, "doctype": CHECK_DOCTYPE})
    except Exception as exc:  # noqa: BLE001 — best-effort cleanup, never masks the result
        print(f"  (cleanup warning: {exc})", file=sys.stderr)


def _fetch_anon(path: str) -> tuple[int, str]:
    """GET `path` with no cookies at all — a real anonymous website visitor,
    not a session this process happens to be holding."""
    pins = load_pins()
    request = urllib.request.Request(
        f"{BASE_URL}/{path.lstrip('/')}",
        headers={"Host": pins.get("SITE_NAME", "sandbox.localhost")},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _require_local(site: str) -> None:
    if site not in LOCAL_ONLY:
        raise CouldNotCheck(
            f"refusing to write a check fixture into {site!r} — this check only ever runs "
            f"against {', '.join(LOCAL_ONLY)} (ADR-002). Check sandbox/pins.env's SITE_NAME."
        )


def _check_editor_renders_map(site: str) -> bool:
    """TRAP-019: extend the map component onto a page and open the editor.

    Builds and tears down a component/page named unmistakably as a check
    fixture — never `location-map`, never a route a real site would use — so
    this can run against a sandbox someone is also using by hand without
    colliding with anything real.
    """
    _require_local(site)

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

    print(f"  {'PASS' if rendered else 'FAIL'}  the map's iframe is in the editor's DOM")
    return rendered


def _menu_blocks(*, data_key: str) -> list[dict]:
    """A repeater reading `data.<data_key>` — the block half of a data-driven
    page, paired with whatever `page_data_script` a caller sets."""
    card = new_block(
        "div",
        children=[
            new_block(
                "span", inner_html="Item",
                dynamic_values=[binding("item_name", "innerHTML")],
            ),
            new_block(
                "span", inner_html="Price",
                dynamic_values=[binding("price", "innerHTML")],
            ),
        ],
    )
    return [repeater(data_key=data_key, child=card, element="main")]


def _check_public_data_script(site: str) -> bool:
    """TRAP-020: `frappe.db.get_all` is public by design; `frappe.db.get_list`
    is not, and 500s the whole page for a real, cookie-less visitor while
    working perfectly for whoever is previewing it as themselves.

    Proves both directions against the same throwaway doctype (System
    Manager read only, deliberately never given Guest access): our own
    `list_data_script()` output must reach a real anonymous visitor, and a
    hand-built `.get_list`-based script — simulating the exact mistake this
    trap is named for — must fail for the same visitor the same way. A check
    that only tried the fixed script could pass by accident if the pin ever
    changed Builder's sandbox; trying the broken one too is what makes this a
    check on the *trap*, not just on our code.
    """
    _require_local(site)
    if not _server_reachable():
        raise CouldNotCheck(
            f"nothing is answering on {BASE_URL} — the bench container is up but its web "
            "server is not. Run `buildsmith sandbox serve` first."
        )

    fixed_route = CHECK_020_ROUTE
    broken_route = f"{CHECK_020_ROUTE}-broken"

    try:
        setup_out = run_bench(
            _DOCTYPE_SETUP % {
                "site": site,
                "doctype": CHECK_DOCTYPE,
                # Built here, not with raw %s inside the template: a
                # backtick-quoted SQL identifier escapes an embedded
                # backtick by doubling it, and repr() alone does not know
                # that — only relying on it once, for a string this module
                # itself controls, keeps that escaping in one place.
                "drop_sql": f"DROP TABLE IF EXISTS `tab{CHECK_DOCTYPE.replace('`', '``')}`",
                "records": [("Grilled Cheese", "5.00"), ("Bowl of Chili", "6.50")],
            }
        )
        if "SAFE_EXEC_ENABLED=True" in setup_out:
            # TRAP-020's whole finding — and list_data_script()'s safety
            # guarantee — is conditional on this being False (see the trap
            # entry). A different value means this check would be testing
            # the wrong sandbox, not that the trap is fixed or broken.
            raise CouldNotCheck(
                "this sandbox has Server Scripts enabled bench-wide "
                "(common_site_config.json), so page_data_script runs in plain "
                "safe_exec, not Builder's safer_exec — TRAP-020's finding does not "
                "apply as documented, and this check cannot validate that "
                "configuration."
            )

        fixed_script = list_data_script(
            target="items", doctype=CHECK_DOCTYPE, fields=["item_name", "price"],
            order_by="item_name",
        )
        broken_script = (
            f'data.items = frappe.db.get_list({CHECK_DOCTYPE!r}, '
            'fields=["item_name", "price"], order_by="item_name")'
        )

        for route, script in ((fixed_route, fixed_script), (broken_route, broken_script)):
            run_bench(
                _PAGE_020_SETUP % {
                    "site": site,
                    "route": route,
                    "blocks_json": json.dumps(_menu_blocks(data_key="items")),
                    "script": script,
                }
            )
        _clear_caches(site)

        fixed_status, fixed_body = _fetch_anon(fixed_route)
        broken_status, _ = _fetch_anon(broken_route)
    finally:
        _teardown_020(site, routes=(fixed_route, broken_route))

    fixed_ok = fixed_status == 200 and "Grilled Cheese" in fixed_body
    print(
        f"  {'PASS' if fixed_ok else 'FAIL'}  frappe.db.get_all reaches an anonymous "
        f"visitor (status {fixed_status})"
    )
    broken_ok = broken_status != 200
    print(
        f"  {'PASS' if broken_ok else 'FAIL'}  frappe.db.get_list 500s the same visitor "
        f"(status {broken_status})"
    )
    if not broken_ok:
        print(
            "  a hand-built script using frappe.db.get_list, with no Guest permission "
            "granted, reached an anonymous visitor without erroring. Either the pin "
            "changed Builder's sandbox permission behaviour, or this doctype somehow "
            "already had Guest read — TRAP-020 may be stale."
        )

    return fixed_ok and broken_ok


def main(argv: list[str] | None = None) -> int:
    require_running()
    pins = load_pins()
    site = pins.get("SITE_NAME", "sandbox.localhost")
    print(f"Builder pin: {pins.builder} ({pins.get('BUILDER_REF_STATUS')})\n")

    try:
        bench_ok, out = _check_bench_traps()
    except Exception as exc:  # noqa: BLE001 - the bench reports its own detail
        print(exc, file=sys.stderr)
        return 1
    sys.stdout.write(out)
    results = [bench_ok]

    # (header, checker, what an unchecked/failed run means)
    checks = [
        (
            "TRAP-019 — a component-extended iframe renders live in the editor",
            lambda: _check_editor_renders_map(site),
            "the component-extended iframe never reached the editor canvas. Either "
            "TRAP-019's fix (root the component on the iframe, not a wrapping div) has "
            "regressed, or the pinned Builder's editor render path has changed.",
        ),
        (
            "TRAP-020 — a public page_data_script reaches a real anonymous visitor",
            lambda: _check_public_data_script(site),
            "see the PASS/FAIL detail above — either frappe.db.get_all stopped being "
            "public by default, or frappe.db.get_list stopped 500ing a Guest visitor. "
            "Either way the pinned Builder's sandbox behaviour has moved.",
        ),
    ]

    unchecked = False
    for header, checker, explanation in checks:
        print(f"\n{header}\n")
        try:
            ok = checker()
        except CouldNotCheck as exc:
            print(f"  ????  COULD NOT CHECK — {exc}")
            unchecked = True
            continue
        except Exception as exc:  # noqa: BLE001 - unexpected is still "found a problem"
            print(f"  ????  COULD NOT CHECK — unexpected error: {exc}", file=sys.stderr)
            results.append(False)
            continue
        results.append(ok)
        if not ok:
            print(f"  {explanation}")

    # "Could not check" must never read as "this is fine" — but a problem
    # already found must keep exit 1: not being able to check one trap never
    # outranks having found a different one actually broken.
    if not all(results):
        return 1
    return 2 if unchecked else 0


if __name__ == "__main__":
    raise SystemExit(main())
