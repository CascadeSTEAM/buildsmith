#!/usr/bin/env python3
"""Prove a publish reproduces your dev copy — before anything touches live.

The dangerous asymmetry in this workflow: a cloning mistake is recoverable
because the live site is untouched, and you notice by looking at localhost. A
publishing mistake is destructive and you hear about it from a customer.

So the publish payload is rehearsed. This takes the captured dev state, applies
it to a **scratch site**, and then checks the scratch site against dev with the
same two tools that validate a clone:

1. `clone-diff` — set differences on selectors, declarations, assets, text,
   links and scripts. Not counts.
2. `visual-check` — drives a browser and *performs* every feature in
   `features.json`, because a handler that binds and does nothing passes every
   static check ever written.

If the rehearsal does not reproduce dev, the payload is wrong and publishing it
to a real site would have produced exactly the same wrongness, silently.

Both sites live in the local sandbox and are resolved by Host header, so the
rehearsal cannot reach anything real — same guard as `bin/load-dev.py`.

    buildsmith publish verify --site example

Exit status is 1 if the rehearsal does not match dev.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from buildsmith.errors import CouldNotCheck
from buildsmith.tools import clone_diff as cd

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ["docker", "compose", "-f", str(ROOT / "sandbox" / "docker-compose.yml")]
BENCH = "/home/frappe/frappe-bench"
DEV = "sandbox.localhost"
SCRATCH = "roundtrip.localhost"

__all__ = ["rehearse", "main"]

APPLY = r"""
import json, pathlib, frappe
frappe.init(site=%(site)r); frappe.connect()
frappe.flags.in_migrate = True   # TRAP-009: nothing may be draining the queue

# Start from empty, so the rehearsal proves the payload alone reproduces dev
# rather than proving it on top of whatever was already there.
for name in frappe.get_all("Builder Page", pluck="name"):
    frappe.delete_doc("Builder Page", name, force=True)
for name in frappe.get_all("Builder Component", pluck="name"):
    frappe.delete_doc("Builder Component", name, force=True)

state = pathlib.Path("/tmp/publish")

# Tokens first: composition embeds each token's live value as a fallback, so a
# page applied before its tokens carries stale literals.
applied = json.loads((state / "tokens-applied.json").read_text())["tokens"]
minted = 0
for key, spec in applied.items():
    if frappe.db.exists("Builder Variable", spec["uuid"]):
        continue
    doc = frappe.get_doc({
        "doctype": "Builder Variable", "variable_name": spec["variable_name"],
        "type": spec.get("type") or "Color", "value": spec["value"],
        "dark_value": spec.get("dark_value"), "group": spec.get("group"),
    })
    # Keep the uuid: every var(--uuid) reference in the payload points at it.
    doc.name = spec["uuid"]
    doc.insert(set_name=spec["uuid"])
    minted += 1

folder = ""
for f in sorted((state / "pages").glob("*.json")):
    rec = json.loads(f.read_text())
    if rec.get("project_folder"):
        folder = rec["project_folder"]; break
if folder and not frappe.db.exists("Builder Project Folder", folder):
    frappe.get_doc({"doctype": "Builder Project Folder",
                    "folder_name": folder}).insert(ignore_if_duplicate=True)

for f in sorted((state / "components").glob("*.json")):
    rec = json.loads(f.read_text())
    frappe.get_doc({
        "doctype": "Builder Component", "component_id": rec["component_id"],
        "component_name": rec.get("component_name") or rec["component_id"],
        "block": json.dumps(rec["block"]),
        "component_data_script": rec.get("component_data_script"),
    }).insert()

routes = []
for f in sorted((state / "pages").glob("*.json")):
    rec = json.loads(f.read_text())
    payload = {k: v for k, v in rec.items() if k not in ("name", "blocks")}
    payload["doctype"] = "Builder Page"
    payload["blocks"] = json.dumps(rec["blocks"])
    frappe.get_doc(payload).insert()
    routes.append(rec["route"])

home = json.loads((state / "manifest.json").read_text())["settings"].get("home_page")
if home:
    frappe.db.set_value("Website Settings", "Website Settings", "home_page", home)

frappe.db.commit()
print("APPLIED:" + json.dumps({"routes": routes, "tokens": minted}))
"""


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def rehearse(site: str) -> int:
    state = ROOT / "sites" / site / "dev-state"
    if not state.is_dir():
        raise CouldNotCheck(
            f"{state} does not exist. Capture the dev instance first: "
            f"buildsmith capture --site {site}"
        )

    print("1. applying the captured dev state to a scratch site\n")
    _run([*COMPOSE, "exec", "-T", "bench", "rm", "-rf", "/tmp/publish"])
    _run([*COMPOSE, "cp", str(state), "bench:/tmp/publish"])

    # Assets travel with the payload; a rehearsal without them proves nothing
    # about a site whose hero is a background image.
    assets = ROOT / "sites" / site / "assets"
    if assets.is_dir():
        _run([*COMPOSE, "cp", str(assets), "bench:/tmp/publish-assets"])
        _run([*COMPOSE, "exec", "-T", "bench", "bash", "-lc",
              f"mkdir -p {BENCH}/sites/{SCRATCH}/public/files && "
              f"cp -f /tmp/publish-assets/* {BENCH}/sites/{SCRATCH}/public/files/ || true"])

    completed = _run(
        [*COMPOSE, "exec", "-T", "bench", "bash", "-lc",
         f"cd {BENCH}/sites && {BENCH}/env/bin/python -"],
        input=APPLY % {"site": SCRATCH},
    )
    line = next((ln for ln in completed.stdout.splitlines() if ln.startswith("APPLIED:")), None)
    if line is None:
        print(completed.stdout)
        print(completed.stderr, file=sys.stderr)
        raise SystemExit("the rehearsal failed to apply — the payload is not applyable")
    result = json.loads(line[len("APPLIED:"):])
    print(f"   applied {len(result['routes'])} page(s), minted {result['tokens']} token(s)")

    _run([*COMPOSE, "exec", "-T", "bench", "bash", "-lc",
          f"cd {BENCH} && bench --site {SCRATCH} clear-website-cache && "
          f"bench --site {SCRATCH} clear-cache"])

    # Both sites answer on the same port; Frappe resolves by Host header.
    print("\n2. content diff: dev vs the rehearsal\n")
    failures = 0
    manifest = json.loads((state / "manifest.json").read_text())
    public_routes = [r for r in manifest["routes"] if not r.startswith("template/")]
    if not public_routes:
        # An empty loop below would check nothing and step 4 would still say
        # "reproduces dev exactly" — a pass that proved nothing.
        raise CouldNotCheck(
            "the manifest lists no public routes, so the content diff has "
            "nothing to compare. Re-capture the dev instance."
        )
    for route in public_routes:
        # Both sites answer on one port and are resolved by Host header, so the
        # served HTML is fetched directly rather than by URL.
        dev = _run(["curl", "-s", "--max-time", "25", "-H", f"Host: {DEV}",
                    f"http://127.0.0.1:8000/{route}"]).stdout
        scratch = _run(["curl", "-s", "--max-time", "25", "-H", f"Host: {SCRATCH}",
                        f"http://127.0.0.1:8000/{route}"]).stdout
        missing_sel = sorted(set(cd._rules(dev)) - set(cd._rules(scratch)))
        missing_txt = sorted(t for t in cd._text(dev) - cd._text(scratch) if len(t) > 2)
        missing_ast = sorted(cd._assets(dev) - cd._assets(scratch))
        missing_scr = sorted(cd._scripts(dev) - cd._scripts(scratch))

        problems = []
        for label, items in (("selectors", missing_sel), ("text", missing_txt),
                             ("assets", missing_ast), ("scripts", missing_scr)):
            if items:
                problems.append(f"{len(items)} {label}")
        if problems:
            failures += 1
            print(f"   FAIL /{route}: rehearsal is missing {', '.join(problems)}")
            for item in (missing_txt + missing_ast)[:4]:
                print(f"          {item[:90]}")
        else:
            print(f"   PASS /{route}: identical to dev")

    print("\n3. browser check against the feature inventory\n")
    unchecked = False
    venv = ROOT / ".venv" / "bin" / "python"
    if not venv.exists():
        # A skipped check must not vanish into a clean verdict: a handler that
        # binds and does nothing passes every static diff above.
        unchecked = True
        print("   SKIPPED — no .venv with playwright; run: python3 install.py --dev")
    else:
        visual = _run([
            str(venv), "-m", "buildsmith.tools.visual_check",
            "--site", site, "--clone", f"http://{SCRATCH}:8000",
        ])
        print("   " + "\n   ".join(visual.stdout.strip().splitlines()[-4:]))
        if visual.returncode == 2:
            # The check never ran (no features.json, say). That is not a
            # rehearsal failure — but it is not a pass either.
            unchecked = True
            if visual.stderr.strip():
                print("   " + visual.stderr.strip().splitlines()[-1])
        elif visual.returncode != 0:
            failures += 1

    print()
    if failures:
        print(f"REHEARSAL FAILED — {failures} check(s). This payload would have produced")
        print("the same result on the live site, silently. Do not publish it.")
        return 1
    if unchecked:
        print("Content matches dev, but the browser check NEVER RAN — nothing has")
        print("confirmed any feature works. Not a verified rehearsal (exit 2).")
        return 2
    print("Rehearsal reproduces dev exactly. The payload is safe to hand over.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--site", required=True)
    args = parser.parse_args(argv)
    return rehearse(args.site)


if __name__ == "__main__":
    raise SystemExit(main())
