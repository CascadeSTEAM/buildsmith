#!/usr/bin/env python3
"""Read the dev instance's current state back into the private layer.

The workflow is: clone the live site into dev, **edit it in Builder**, approve
it, publish. That middle step means the dev instance — not this repo — is the
source of truth for what you actually want. Anything not captured from it is
something the publish will silently drop.

So this reads back, rather than assuming our last build is still accurate:

    sites/<site>/dev-state/pages/*.json        every Builder Page, with blocks
    sites/<site>/dev-state/components/*.json   every Builder Component
    sites/<site>/dev-state/tokens-applied.json the live token map, uuids and all
    sites/<site>/dev-state/manifest.json       counts and a content hash

The hash is what later steps compare against, so "the dev copy changed under us
while we were publishing" is detectable rather than assumed away.

Reads from the local dev instance only — the same localhost-only guard as
`bin/load-dev.py`, for the same reason.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ["docker", "compose", "-f", str(ROOT / "sandbox" / "docker-compose.yml")]
BENCH = "/home/frappe/frappe-bench"
LOCAL_ONLY = ("sandbox.localhost", "roundtrip.localhost")

__all__ = ["capture", "main"]

READ = r"""
import json, frappe
frappe.init(site=%(site)r); frappe.connect()

out = {"pages": [], "components": [], "tokens": {}, "settings": {}}

for name in frappe.get_all("Builder Page", pluck="name"):
    d = frappe.get_doc("Builder Page", name)
    out["pages"].append({
        "name": d.name, "page_title": d.page_title, "route": d.route,
        "published": int(d.published or 0), "is_template": int(d.is_template or 0),
        "template_group": d.template_group, "dynamic_route": int(d.dynamic_route or 0),
        "project_folder": d.project_folder, "favicon": d.favicon,
        "head_html": d.head_html or "", "body_html": d.body_html or "",
        "meta_description": d.meta_description,
        "blocks": frappe.parse_json(d.blocks or "[]"),
        "draft_blocks": frappe.parse_json(d.draft_blocks or "[]"),
    })

for name in frappe.get_all("Builder Component", pluck="name"):
    d = frappe.get_doc("Builder Component", name)
    out["components"].append({
        "component_id": d.component_id, "component_name": d.component_name,
        "block": frappe.parse_json(d.block or "{}"),
        "component_data_script": d.component_data_script,
    })

for v in frappe.get_all("Builder Variable",
                        fields=["name","variable_name","type","value","dark_value","group"]):
    # Keyed by UUID, NOT by variable_name. Builder scopes variable names per
    # group, so names collide freely: a real site had 167 variables under 112
    # distinct names, and keying on the name silently discarded 55 of them and
    # made the result depend on iteration order.
    out["tokens"][v.name] = {
        "uuid": v.name, "value": v.value, "dark_value": v.dark_value,
        "type": v.type, "group": v.group, "variable_name": v.variable_name,
    }

out["settings"]["home_page"] = frappe.db.get_value(
    "Website Settings", "Website Settings", "home_page")

print("STATE:" + json.dumps(out))
"""


def _content_hash(state: dict) -> str:
    """A hash of what matters, so drift in the dev copy is detectable.

    Deliberately excludes names and timestamps: a page's name is an
    unchooseable hash (TRAP-012) and would make every capture look different.
    """
    material = {
        "pages": sorted(
            (p["route"], p["page_title"], json.dumps(p["blocks"], sort_keys=True),
             p.get("head_html", ""),
             # An unpublished draft is still damage waiting to be published
             # (simulate.pages_using checks it explicitly) — a drift check
             # that can't see a draft edit isn't checking what matters.
             json.dumps(p.get("draft_blocks") or [], sort_keys=True))
            for p in state["pages"]
        ),
        "components": sorted(
            (c["component_id"], json.dumps(c["block"], sort_keys=True))
            for c in state["components"]
        ),
        # The uuid is the key, so the variable_name must be hashed explicitly or
        # a rename would not register as a change.
        "tokens": sorted(
            (k, v.get("variable_name") or "", v["value"], v.get("dark_value") or "")
            for k, v in state["tokens"].items()
        ),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]


#: Page fields we keep. Named explicitly rather than taking whatever the doctype
#: happens to expose, so a Builder upgrade that adds a field does not silently
#: change what a capture means — and so both transports return the same shape.
PAGE_FIELDS = (
    "name", "page_title", "route", "published", "is_template", "template_group",
    "dynamic_route", "project_folder", "favicon", "head_html", "body_html",
    "meta_description", "blocks", "draft_blocks",
)
_PAGE_INTS = ("published", "is_template", "dynamic_route")
_PAGE_STRS = ("head_html", "body_html")


def _read_state_bench(target: str) -> dict:
    """Read the dev instance by executing inside its container."""
    running = subprocess.run(
        [*COMPOSE, "ps", "--status", "running", "--services"], capture_output=True, text=True
    )
    if "bench" not in running.stdout.split():
        raise SystemExit("the dev instance is not running. Start it with: buildsmith sandbox up")

    completed = subprocess.run(
        [*COMPOSE, "exec", "-T", "bench", "bash", "-lc",
         f"cd {BENCH}/sites && {BENCH}/env/bin/python -"],
        input=READ % {"site": target}, text=True, capture_output=True,
    )
    line = next((ln for ln in completed.stdout.splitlines() if ln.startswith("STATE:")), None)
    if line is None:
        print(completed.stdout)
        print(completed.stderr, file=sys.stderr)
        raise SystemExit("could not read the dev instance")
    return json.loads(line[len("STATE:"):])


def _read_state_rest(target: str) -> dict:
    """Read the dev instance over HTTP, for a container with no Docker socket.

    Must return a structure identical to `_read_state_bench`, because both feed
    the same content hash — a transport that returned a subtly different shape
    would make every capture look like drift. `tests/test_capture_transport.py`
    asserts the two agree.
    """
    from buildsmith.tools.frappe_client import from_env

    client = from_env(site=target)
    out: dict = {"pages": [], "components": [], "tokens": {}, "settings": {}}

    for row in client.get_list("Builder Page", fields=list(PAGE_FIELDS)):
        page = {k: row.get(k) for k in PAGE_FIELDS}
        # bench read these through the ORM, which coerces. REST returns whatever
        # is in the column, so normalise here or the hashes diverge on types.
        for key in _PAGE_INTS:
            page[key] = int(page.get(key) or 0)
        for key in _PAGE_STRS:
            page[key] = page.get(key) or ""
        page["blocks"] = json.loads(row.get("blocks") or "[]")
        # An unpublished draft is still damage waiting to be published
        # (simulate.pages_using checks it explicitly) — capture it too.
        page["draft_blocks"] = json.loads(row.get("draft_blocks") or "[]")
        out["pages"].append(page)

    for row in client.get_list(
        "Builder Component",
        fields=["component_id", "component_name", "block", "component_data_script"],
    ):
        out["components"].append({
            "component_id": row.get("component_id"),
            "component_name": row.get("component_name"),
            "block": json.loads(row.get("block") or "{}"),
            "component_data_script": row.get("component_data_script"),
        })

    for row in client.get_list(
        "Builder Variable",
        fields=["name", "variable_name", "type", "value", "dark_value", "group"],
    ):
        out["tokens"][row["name"]] = {
            "uuid": row["name"], "value": row.get("value"),
            "dark_value": row.get("dark_value"), "type": row.get("type"),
            "group": row.get("group"), "variable_name": row["variable_name"],
        }

    out["settings"]["home_page"] = client.get(
        "Website Settings", "Website Settings"
    ).get("home_page")
    return out


def read_state(target: str, *, transport: str = "auto") -> dict:
    """Read the dev instance over whichever transport is available.

    `auto` prefers REST when a token is present, because that is the path the
    container takes and the one that needs the exercise. It falls back to bench
    rather than failing, so a developer with no token set keeps working.
    """
    if target not in LOCAL_ONLY:
        raise SystemExit(
            f"REFUSED: '{target}' is not a local dev site. This reads only from "
            f"{', '.join(LOCAL_ONLY)}."
        )
    if transport == "auto":
        transport = "rest" if os.environ.get("BUILDSMITH_FRAPPE_TOKEN") else "bench"
    state = _read_state_rest(target) if transport == "rest" else _read_state_bench(target)

    # Order deterministically, at the source rather than at each consumer. The
    # two transports list documents in whatever order their query returned, and
    # those orders differ. `_content_hash` sorts, so it was already stable — but
    # the files written to dev-state/ were not, which made captures of an
    # unchanged site produce spurious diffs. Sorting by name rather than route
    # because route can be empty and names are unique (TRAP-012).
    state["pages"].sort(key=lambda p: p["name"])
    state["components"].sort(key=lambda c: c["component_id"])
    state["tokens"] = dict(sorted(state["tokens"].items()))
    return state


def _filename_key(value: str, *, field: str) -> str:
    """Guard a doctype field before it becomes a `dev-state/` filename.

    `page['name']` is normally a Frappe-minted `page-<hash8>` (TRAP-012), but
    ADR-008 records that a bench import can make it choosable — and
    `component_id` is chosen by the author outright (TRAP-005). Either could
    in principle contain a path separator; writing that straight into a path
    would crash `write_text()` (no intermediate directory exists) or, worse,
    write outside `dev-state/` silently. Fail loud instead of reproducing the
    exact class of silent clobber #27 was filed to close.
    """
    if not value or "/" in value or "\\" in value or ".." in value:
        raise SystemExit(f"capture_dev: unsafe {field} for a dev-state filename: {value!r}")
    return value


def capture(site: str, *, target: str = "sandbox.localhost",
            transport: str = "auto", out: Path | None = None) -> dict:
    """`out` overrides the destination — the optimize workflow checkpoints
    into its own immutable directory instead of the shared dev-state/."""
    resolved = transport
    if resolved == "auto":
        resolved = "rest" if os.environ.get("BUILDSMITH_FRAPPE_TOKEN") else "bench"
    state = read_state(target, transport=resolved)

    out = out or ROOT / "sites" / site / "dev-state"
    (out / "pages").mkdir(parents=True, exist_ok=True)
    (out / "components").mkdir(parents=True, exist_ok=True)

    for existing in list((out / "pages").glob("*.json")) + list(
        (out / "components").glob("*.json")
    ):
        existing.unlink()

    for page in state["pages"]:
        # Keyed by `name`, the doctype's own unique field (TRAP-012) — not by
        # route. Two distinct routes can slugify to the same string (`a/b` and
        # `a_b`, or a literal route `home` colliding with the empty-route
        # fallback), and a route-derived filename silently overwrote one
        # page's capture with another's (#27).
        name = _filename_key(page["name"], field="page name")
        (out / "pages" / f"{name}.json").write_text(json.dumps(page, indent=2) + "\n")
    for component in state["components"]:
        component_id = _filename_key(component["component_id"], field="component_id")
        (out / "components" / f"{component_id}.json").write_text(
            json.dumps(component, indent=2) + "\n"
        )
    (out / "tokens-applied.json").write_text(
        json.dumps({"tokens": state["tokens"]}, indent=2) + "\n"
    )

    manifest = {
        "site": site,
        "captured_from": target,
        "transport": resolved,
        "content_hash": _content_hash(state),
        "counts": {
            "pages": len(state["pages"]),
            "components": len(state["components"]),
            "tokens": len(state["tokens"]),
        },
        "settings": state["settings"],
        "routes": sorted(p["route"] for p in state["pages"]),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--site", required=True)
    parser.add_argument("--target", default="sandbox.localhost")
    parser.add_argument("--transport", choices=["auto", "rest", "bench"],
                        default="auto")
    args = parser.parse_args(argv)

    manifest = capture(args.site, target=args.target, transport=args.transport)
    print(f"captured dev state for '{args.site}' from {manifest['captured_from']}")
    for what, count in sorted(manifest["counts"].items()):
        print(f"  {what}: {count}")
    print(f"  routes: {', '.join('/' + r for r in manifest['routes'])}")
    print(f"  content hash: {manifest['content_hash']}")
    print(f"\nwritten to sites/{args.site}/dev-state/ (private layer, gitignored)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
