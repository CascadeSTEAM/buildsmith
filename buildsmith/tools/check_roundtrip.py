#!/usr/bin/env python3
"""Prove the `builder_files/` round-trip actually works at the pin.

ADR-003 deferred this on the belief it did not exist. ADR-004 withdrew that,
having found `export_import_standard_page.py` predates the version string we
were reading. Neither settles whether it *works* — so this does it.

Author a page on one site with `is_standard=1` and an `app`, let `on_update`
export it to `<app>/builder_files/`, then install/migrate a second site and
check the page reproduces with its blockIds intact. blockIds are the part that
matters: without them the fixture is a picture of a page rather than a page
(TRAP-001).

Needs the sandbox, and creates a second site in it the first time. Both sites
are disposable; `buildsmith sandbox destroy` removes them.

    bash sandbox/roundtrip-check.sh
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ["docker", "compose", "-f", str(ROOT / "sandbox" / "docker-compose.yml")]
SECOND_SITE = "roundtrip.localhost"

BLOCKS = [
    {
        "blockId": "rt-root",
        "element": "div",
        "children": [
            {"blockId": "rt-h1", "element": "h1", "innerHTML": "Round Trip Proof"},
            {"blockId": "rt-p", "element": "p", "innerHTML": "Authored, exported, reimported."},
        ],
    }
]

# The realistic case, not a bare page: a token, a component that references it,
# and a page that carries a shell extending that component. This is where the
# round trip either holds together or does not — the uuid Builder assigned the
# variable on site 1 has to be the uuid site 2 ends up with, or every
# `var(--uuid)` reference in the imported markup resolves to nothing.
AUTHOR = """
import json, frappe
frappe.init(site="sandbox.localhost"); frappe.connect()

# TRAP-009, encountered for real while writing this check. BuilderComponent
# .on_update calls queue_action("clear_page_cache"), which enqueues a job AND
# locks the document. The sandbox runs no workers by design, so the job never
# runs and the lock never clears — the next save fails with DocumentLockedError.
# Builder already guards that call behind the system-activity flags, so setting
# one is the supported way to author without a worker. It is not a workaround
# for production: there, the answer is workers.
frappe.flags.in_migrate = True

for old in frappe.get_all("Builder Page", filters={"route": "roundtrip-proof"}, pluck="name"):
    frappe.delete_doc("Builder Page", old, force=True)
for old in frappe.get_all("Builder Component", filters={"component_id": "rt-header"}, pluck="name"):
    frappe.delete_doc("Builder Component", old, force=True)
for old in frappe.get_all("Builder Variable", filters={"variable_name": "RT Brand"}, pluck="name"):
    frappe.delete_doc("Builder Variable", old, force=True)

variable = frappe.get_doc({
    "doctype": "Builder Variable", "variable_name": "RT Brand",
    "type": "Color", "value": "#0a7d55", "dark_value": "#3fbf8c", "group": "roundtrip",
}).insert()

component = frappe.get_doc({
    "doctype": "Builder Component", "component_id": "rt-header",
    "component_name": "RT Header",
    "block": json.dumps({
        "blockId": "rt-c-root", "element": "header",
        "baseStyles": {"backgroundColor": "var(--%s, #0a7d55)" % variable.name},
        "children": [{"blockId": "rt-c-nav", "element": "nav", "innerHTML": "RT Nav"}],
    }),
}).insert()

page_blocks = [{
    "blockId": "rt-root", "element": "div",
    "children": [
        {"blockId": "rt-shell", "referenceBlockId": "rt-c-root",
         "extendedFromComponent": component.component_id, "element": None,
         "children": [{"blockId": "rt-shell-nav", "referenceBlockId": "rt-c-nav",
                       "element": None, "children": []}]},
        {"blockId": "rt-h1", "element": "h1", "innerHTML": "Round Trip Proof",
         "baseStyles": {"color": "var(--%s, #0a7d55)" % variable.name}},
    ],
}]

doc = frappe.get_doc({
    "doctype": "Builder Page", "page_title": "Round Trip Proof",
    "route": "roundtrip-proof", "blocks": json.dumps(page_blocks),
    "published": 1, "is_standard": 1, "app": "builder",
}).insert()
frappe.db.commit()
print("AUTHORED:" + json.dumps({
    "name": doc.name, "variable_uuid": variable.name,
    "component_id": component.component_id,
}))
"""

VERIFY = r"""
import json, frappe
frappe.init(site="roundtrip.localhost"); frappe.connect()
from builder.builder.doctype.builder_page.builder_page import get_block_html

rows = frappe.get_all("Builder Page", filters={"route": "roundtrip-proof"},
                      fields=["name", "page_title", "blocks"])
out = {"found": len(rows)}
if rows:
    blocks = frappe.parse_json(rows[0].blocks or "[]")
    ids, texts = [], []
    def walk(bs):
        for b in bs:
            if b.get("blockId"): ids.append(b["blockId"])
            if b.get("innerHTML"): texts.append(b["innerHTML"])
            walk(b.get("children") or [])
    walk(blocks)
    out.update({"name": rows[0].name, "title": rows[0].page_title,
                "block_ids": ids, "texts": texts})
    out["variables"] = frappe.get_all("Builder Variable",
                                      filters={"variable_name": "RT Brand"}, pluck="name")
    out["components"] = frappe.get_all("Builder Component",
                                       filters={"component_id": "rt-header"}, pluck="name")
    # The real question: does the page still render the component's content and
    # a resolvable token reference on a site that imported both from files?
    html, css = get_block_html(blocks)[0], get_block_html(blocks)[1]
    out["renders_component_content"] = "RT Nav" in html
    # Token references are emitted into the <style> block, never the markup.
    out["css_var_refs"] = sorted(set(
        __import__("re").findall(r"var\(--([0-9a-f-]{8,})", css)
    ))
print("VERIFIED:" + json.dumps(out))
"""


def _clean_slate_script(second_site: str = SECOND_SITE) -> str:
    """The python script that clears fixtures before AUTHOR runs.

    A pure string builder, factored out so #20's fix — sandbox.localhost
    keeps only its own named fixtures, never a blanket Variable/Component
    wipe — is a single thing to pin in a test, without mocking the whole
    docker-compose subprocess chain that surrounds it.
    """
    return (
        "import frappe\n"
        # sandbox.localhost (#20): NOT a blanket wipe. It is the shared
        # bench every optimize transform targets by default, so deleting
        # every Variable/Component there took unrelated in-progress
        # optimize state with it. AUTHOR (below) already deletes exactly
        # its own fixtures by name/id/route on this site — nothing more
        # is needed here.
        "try:\n"
        "    frappe.init(site='sandbox.localhost'); frappe.connect()\n"
        "    for n in frappe.get_all('Builder Page',"
        " filters={'route': 'roundtrip-proof'}, pluck='name'):\n"
        "        frappe.delete_doc('Builder Page', n, force=True)\n"
        "    frappe.db.commit(); frappe.destroy()\n"
        "except Exception:\n"
        "    pass\n"
        # roundtrip.localhost: disposable scratch, also used by
        # publish-verify — a blanket wipe here is safe and prevents a
        # false "tokens arrived" reading from a previous run's leftovers.
        f"try:\n"
        f"    frappe.init(site={second_site!r}); frappe.connect()\n"
        "    for n in frappe.get_all('Builder Page',"
        " filters={'route': 'roundtrip-proof'}, pluck='name'):\n"
        "        frappe.delete_doc('Builder Page', n, force=True)\n"
        "    for n in frappe.get_all('Builder Variable', pluck='name'):\n"
        "        frappe.delete_doc('Builder Variable', n, force=True)\n"
        "    for n in frappe.get_all('Builder Component', pluck='name'):\n"
        "        frappe.delete_doc('Builder Component', n, force=True)\n"
        "    frappe.db.commit(); frappe.destroy()\n"
        "except Exception:\n"
        "    pass\n"
    )


def run(script: str, marker: str) -> dict:
    completed = subprocess.run(
        [*COMPOSE, "exec", "-T", "bench", "bash", "-lc",
         "cd /home/frappe/frappe-bench/sites && /home/frappe/frappe-bench/env/bin/python -"],
        input=script, text=True, capture_output=True,
    )
    line = next((ln for ln in completed.stdout.splitlines() if ln.startswith(marker)), None)
    if line is None:
        print(completed.stdout)
        print(completed.stderr, file=sys.stderr)
        raise SystemExit(f"expected {marker} in the output")
    return json.loads(line[len(marker):])


def main(argv: list[str] | None = None) -> int:
    from buildsmith.workflows.optimize import gates

    # #20: this check's clean-slate step used to wipe every Builder Variable
    # and Component on sandbox.localhost, taking any in-progress optimize
    # state with it while the gate ledger still called it applied+proved.
    # The wipe below is now scoped off sandbox.localhost entirely (see the
    # clean-slate step), but refuse loudly first anyway — the same guard
    # `optimize baseline` uses before it would absorb an unproven state.
    open_ledgers = gates.any_pending()
    if open_ledgers:
        names = ", ".join(
            f"{site} ({gates.transform_names(entries)})"
            for site, entries in sorted(open_ledgers.items())
        )
        raise SystemExit(
            f"the shared sandbox has applied transform(s) with no passing "
            f"oracle: {names}. check_roundtrip mutates that sandbox and "
            "could collide with unproved state. Run `buildsmith optimize "
            "oracle` first, or `buildsmith optimize status` to see what's "
            "pending."
        )

    running = subprocess.run([*COMPOSE, "ps", "--status", "running", "--services"],
                             capture_output=True, text=True)
    if "bench" not in running.stdout.split():
        raise SystemExit("the sandbox is not running. Start it with: buildsmith sandbox up")

    # Start from a clean slate. This matters more than it looks: page names are
    # `page-<hash8>` and unchooseable, so re-authoring the "same" page produces a
    # NEW fixture directory while the old one stays. Nothing prunes it, so both
    # import on the next migrate and the target site ends up with two published
    # pages racing for one route (TRAP-010, TRAP-012). Discovered by this very
    # check leaving litter behind on its first run.
    print("0. clearing stale fixtures, pages, variables and document locks")
    # A lock left by an earlier queue_action outlives the process (TRAP-009).
    subprocess.run(
        [*COMPOSE, "exec", "-T", "bench", "bash", "-lc",
         "rm -f /home/frappe/frappe-bench/sites/*/locks/*.lock 2>/dev/null || true"],
        capture_output=True,
    )
    subprocess.run(
        [*COMPOSE, "exec", "-T", "bench", "bash", "-lc",
         "rm -rf /home/frappe/frappe-bench/apps/builder/builder/builder_files/pages/page_*"],
        check=True, capture_output=True,
    )
    subprocess.run(
        [*COMPOSE, "exec", "-T", "bench", "bash", "-lc", f"""
        cd /home/frappe/frappe-bench
        for s in sandbox.localhost {SECOND_SITE}; do
          [ -d sites/$s ] && bench --site $s execute frappe.client.delete \
            --args "['Builder Page','__none__']" >/dev/null 2>&1 || true
        done
        """],
        capture_output=True,
    )
    subprocess.run(
        [*COMPOSE, "exec", "-T", "bench", "bash", "-lc",
         "cd /home/frappe/frappe-bench/sites && /home/frappe/frappe-bench/env/bin/python - "],
        input=_clean_slate_script(),
        text=True, capture_output=True,
    )

    print("1. authoring a standard page on sandbox.localhost")
    authored = run(AUTHOR, "AUTHORED:")
    print(f"   created {authored['name']}")

    print("2. checking the export landed in builder_files/")
    listing = subprocess.run(
        [*COMPOSE, "exec", "-T", "bench", "bash", "-lc",
         "find /home/frappe/frappe-bench/apps/builder/builder/builder_files/pages "
         "-name '*.json' 2>/dev/null | head -20"],
        capture_output=True, text=True,
    )
    files = [ln for ln in listing.stdout.splitlines() if ln.strip()]
    if not files:
        raise SystemExit("nothing was exported — is developer_mode on?")
    for path in files:
        print(f"   {path.split('builder_files/')[-1]}")

    print(f"3. syncing {SECOND_SITE} (creating it if needed)")
    subprocess.run(
        [*COMPOSE, "exec", "-T", "bench", "bash", "-lc", f"""
        set -e
        cd /home/frappe/frappe-bench
        if [ ! -d sites/{SECOND_SITE} ]; then
          bench new-site {SECOND_SITE} --db-root-password 123 --admin-password admin \
            --mariadb-user-host-login-scope='%' >/dev/null 2>&1 || \
          bench new-site {SECOND_SITE} --mariadb-root-password 123 --admin-password admin \
            --no-mariadb-socket >/dev/null 2>&1
          bench --site {SECOND_SITE} install-app builder >/dev/null 2>&1
        else
          bench --site {SECOND_SITE} migrate >/dev/null 2>&1
        fi
        """],
        check=True, capture_output=True,
    )

    print("4. verifying the page reproduced")
    result = run(VERIFY, "VERIFIED:")

    expected_ids = ["rt-root", "rt-shell", "rt-shell-nav", "rt-h1"]
    checks = [
        ("the page exists on the second site", result["found"] == 1),
        ("its name survived the round trip", result.get("name") == authored["name"]),
        ("its title survived", result.get("title") == "Round Trip Proof"),
        # The part that decides whether a fixture is a page or a picture of one.
        ("blockIds survived intact", result.get("block_ids") == expected_ids),
        ("content survived", "Round Trip Proof" in (result.get("texts") or [])),
        ("the component came across", result.get("components") == ["rt-header"]),
        ("the component's content renders on the second site",
         bool(result.get("renders_component_content"))),
        ("the page still references the same token uuid",
         result.get("css_var_refs") == [authored["variable_uuid"]]),
        # Asserting the BUG, deliberately. export_variables() looks a variable up
        # by variable_name, and by `name` with hyphens swapped for underscores —
        # neither of which can ever match a uuid-named record. Upstream's own
        # refactor_builder_variables migration made every variable uuid-named and
        # this function was not updated, so tokens are silently omitted from the
        # export. If this check starts failing, upstream fixed it: delete the
        # inversion and update ADR-005 and TRAP-013.
        ("tokens do NOT survive the round trip (upstream bug, TRAP-013)",
         result.get("variables") == []),
    ]

    print()
    failed = 0
    for label, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        failed += not ok
    print()
    if failed:
        print(f"ROUND TRIP DOES NOT WORK — {failed} check(s) failed at this pin.")
        print(f"observed: {json.dumps(result, indent=2)}")
        return 1
    print(f"Round trip behaves as recorded at this pin — {len(checks)}/{len(checks)} checks.")
    print("Pages and components round-trip with their blockIds intact.")
    print("TOKENS DO NOT — every var(--uuid) reference arrives pointing at a")
    print("variable that does not exist, so the page renders on its literal")
    print("fallbacks and looks almost right. See ADR-005 and TRAP-013.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
