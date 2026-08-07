#!/usr/bin/env python3
"""Load a built site into the **local dev instance**. Localhost only, by force.

Everything else in `bin/` emits files and stops, because applying is an action
and actions belong to the operations project (ADR-002). This is the one
exception, and it is bounded by construction rather than by convention:

**It refuses any target that is not the local sandbox.** Not a warning, not a
flag you can pass — the check runs before anything else and there is no override.
The sandbox is a disposable local container, which is the deliberate carve-out
this project already makes for `sandbox/`. A live site is somebody else's job and
this tool cannot reach one.

What it loads, in the order that works:

1. **Assets**, into the site's public files, so `/files/<name>` resolves. A clone
   whose images 404 is a wireframe, not a clone.
2. **Components**, before the pages that reference them.
3. **Pages**, replacing any at the same route.
4. **Prerequisites** — the project folder and `Website Settings.home_page` —
   because a page that needs them fails without them (TRAP-014).
5. **Both caches**, because a replaced page serves stale until they are cleared,
   and the failure looks like a permission error rather than a stale one
   (TRAP-015). `clear-website-cache` alone is not enough; the rendered page cache
   is separate.

    buildsmith load dev --site example
    buildsmith load dev --site example --no-assets
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from buildsmith.errors import CouldNotCheck

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ["docker", "compose", "-f", str(ROOT / "sandbox" / "docker-compose.yml")]
BENCH = "/home/frappe/frappe-bench"

#: The only hostnames this tool will ever write to. A site name that is not one
#: of these is refused outright — there is deliberately no override, because an
#: override is how "local only" becomes "local by default".
LOCAL_ONLY = ("sandbox.localhost", "roundtrip.localhost")

__all__ = ["load", "main"]


def _refuse_non_local(site_name: str) -> None:
    if site_name not in LOCAL_ONLY:
        raise SystemExit(
            f"REFUSED: '{site_name}' is not a local dev site.\n"
            f"  This tool writes only to {', '.join(LOCAL_ONLY)} — a disposable local\n"
            "  container. Applying to any real site is an action, and actions go through\n"
            "  the operations project (ADR-002). There is no override flag, on purpose.\n"
            "  Emit payloads with `buildsmith build` and hand them over with `buildsmith handoff`."
        )


def _bench(script: str, *, site: str) -> str:
    completed = subprocess.run(
        [*COMPOSE, "exec", "-T", "bench", "bash", "-lc",
         f"cd {BENCH}/sites && {BENCH}/env/bin/python -"],
        input=script, text=True, capture_output=True,
    )
    if completed.returncode != 0:
        print(completed.stdout)
        print(completed.stderr, file=sys.stderr)
        raise SystemExit("the dev instance rejected the load")
    return completed.stdout


def _sandbox_running() -> None:
    running = subprocess.run(
        [*COMPOSE, "ps", "--status", "running", "--services"], capture_output=True, text=True
    )
    if "bench" not in running.stdout.split():
        raise CouldNotCheck(
            "the dev instance is not running. Start it with: buildsmith sandbox up"
        )


LOAD = r"""
import json, pathlib, frappe
frappe.init(site=%(site)r); frappe.connect()
# TRAP-009: no workers may be draining the queue, and queue_action would lock
# every document it touches. Builder guards that behind the system flags.
frappe.flags.in_migrate = True

folder = %(folder)r
if folder and not frappe.db.exists("Builder Project Folder", folder):
    frappe.get_doc({"doctype": "Builder Project Folder",
                    "folder_name": folder}).insert(ignore_if_duplicate=True)
    print("prerequisite: created Builder Project Folder", folder)

# --- assets -----------------------------------------------------------------
loaded = 0
for path in sorted(pathlib.Path("/tmp/devload/assets").glob("*")) if %(assets)s else []:
    if not path.is_file():
        continue
    url = "/files/" + path.name
    if not frappe.db.exists("File", {"file_url": url}):
        frappe.get_doc({"doctype": "File", "file_name": path.name,
                        "file_url": url, "is_private": 0}).insert(ignore_permissions=True)
    loaded += 1
print("assets registered:", loaded)

# --- components before the pages that reference them ------------------------
comp_dir = pathlib.Path("/tmp/devload/components")
for f in sorted(comp_dir.glob("*.json")) if comp_dir.is_dir() else []:
    rec = json.loads(f.read_text()); cid = rec["component_id"]
    if frappe.db.exists("Builder Component", cid):
        doc = frappe.get_doc("Builder Component", cid)
        doc.block = json.dumps(rec["block"]); doc.save()
    else:
        rec["block"] = json.dumps(rec["block"]); frappe.get_doc(rec).insert()
print("components:", cid if 'cid' in dir() else 0)

# --- pages ------------------------------------------------------------------
routes = []
page_dir = pathlib.Path("/tmp/devload/pages")
for f in sorted(page_dir.glob("*.json")) if page_dir.is_dir() else []:
    rec = json.loads(f.read_text())
    route = rec.get("route", "")
    for old in frappe.get_all("Builder Page", filters={"route": route}, pluck="name"):
        frappe.delete_doc("Builder Page", old, force=True)
    # Page scripts travel inside head_html, where the source keeps them.
    # Builder Client Script records look like the native home but only render
    # from a `body` block, which a Builder page rooted on a div never has.
    rec.pop("_client_scripts", None)
    rec["blocks"] = json.dumps(rec["blocks"])
    rec["published"] = 1
    frappe.get_doc(rec).insert()
    routes.append(route)
print("pages:", json.dumps(routes))

if "home" in routes:
    frappe.db.set_value("Website Settings", "Website Settings", "home_page", "home")
    print("prerequisite: Website Settings.home_page = home")

frappe.db.commit()
"""


def load(site: str, *, with_assets: bool = True, target: str = "sandbox.localhost") -> None:
    _refuse_non_local(target)
    _sandbox_running()

    build = ROOT / "sites" / site / "build"
    if not build.is_dir():
        raise CouldNotCheck(
            f"{build} does not exist. Run `buildsmith build --site {site}` first."
        )

    assets = ROOT / "sites" / site / "assets"
    folder = ""
    pages = sorted((build / "pages").glob("*.json")) if (build / "pages").is_dir() else []
    for path in pages:
        record = json.loads(path.read_text())
        if record.get("project_folder"):
            folder = record["project_folder"]
            break

    subprocess.run([*COMPOSE, "exec", "-T", "bench", "rm", "-rf", "/tmp/devload"],
                   capture_output=True)
    subprocess.run([*COMPOSE, "cp", str(build), "bench:/tmp/devload"],
                   check=True, capture_output=True)

    if with_assets and assets.is_dir():
        subprocess.run([*COMPOSE, "cp", str(assets), "bench:/tmp/devload/assets"],
                       check=True, capture_output=True)
        # Frappe serves sites/<site>/public/files/<name> at /files/<name>.
        subprocess.run(
            [*COMPOSE, "exec", "-T", "bench", "bash", "-lc",
             f"mkdir -p {BENCH}/sites/{target}/public/files && "
             f"cp -f /tmp/devload/assets/* {BENCH}/sites/{target}/public/files/ "
             "2>/dev/null || true"],
            check=True, capture_output=True,
        )

    print(
        _bench(
            LOAD % {"site": target, "folder": folder,
                    "assets": bool(with_assets and assets.is_dir())},
            site=target,
        ).strip()
    )

    # Both caches. The route cache and the rendered page cache are separate, and
    # clearing only the website one leaves pages serving stale (TRAP-015).
    subprocess.run(
        [*COMPOSE, "exec", "-T", "bench", "bash", "-lc",
         f"cd {BENCH} && bench --site {target} clear-website-cache && "
         f"bench --site {target} clear-cache"],
        capture_output=True,
    )
    print("caches cleared (website + rendered page)")
    print("\nbrowse: http://127.0.0.1:8000/")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--site", required=True, help="site under sites/")
    parser.add_argument("--target", default="sandbox.localhost")
    parser.add_argument("--no-assets", action="store_true")
    args = parser.parse_args(argv)

    load(args.site, with_assets=not args.no_assets, target=args.target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
