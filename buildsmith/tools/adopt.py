"""Adopt an exported Builder site into the local dev instance — exactly.

This is the *maintain* path (ADR-008). The source is already a Frappe Builder
site, so its authoritative `Builder Page.blocks` exist as records and there is
nothing to reconstruct. Copy the records; anything that differs afterwards is a
bug, not a judgement call.

Contrast with the *import* path (`workflows/replicate`), which scrapes a
non-Frappe site's rendered HTML. That one produces a **new** site that resembles
the old one, and reconstruction loss is expected and reported. Using the import
path against a Frappe source is what produced BS-022 and a long tail of
"completeness" failures: it round-tripped block JSON through rendered HTML and
back, losing everything HTML cannot express.

The export itself is performed by the operations project, never from here
(ADR-002): applying and reading a live system is an action. This consumes the
files it hands over.

    buildsmith adopt --site example
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
SCRIPT = Path(__file__).parent / "bench_scripts" / "adopt.py"

#: The only sites this will ever write to. Same allow-list as everything else,
#: same absence of an override (ADR-007).
LOCAL_ONLY = ("sandbox.localhost", "roundtrip.localhost")

#: Load order is a dependency order, not a preference. A page referencing a
#: component that does not exist yet fails validation; a component styled with a
#: variable that does not exist yet renders on its literal fallback and looks
#: almost right, which is worse.
ORDER = (
    ("Builder Variable", "builder-variable.json"),
    ("Builder Project Folder", "builder-project-folder.json"),
    ("Builder Component", "builder-component.json"),
    ("Builder Client Script", "builder-client-script.json"),
    ("Builder Page", "builder-page.json"),
)

__all__ = ["load_export", "main"]


def _refuse_non_local(target: str) -> None:
    if target not in LOCAL_ONLY:
        raise SystemExit(
            f"REFUSED: '{target}' is not a local dev site.\n"
            f"  This writes only to {', '.join(LOCAL_ONLY)}. Applying to a real site is\n"
            "  an action and goes through the operations project (ADR-002). There is no\n"
            "  override flag, on purpose."
        )


def load_export(export_dir: Path, *, templates: bool) -> tuple[list, str]:
    """Read the exported doctypes in dependency order."""
    doctypes = export_dir / "doctypes"
    if not doctypes.is_dir():
        raise CouldNotCheck(
            f"no export at {doctypes}.\n"
            "  An export is produced by the operations project against the live site;\n"
            "  this tool consumes it and never fetches one itself (ADR-002)."
        )

    order = []
    for doctype, filename in ORDER:
        path = doctypes / filename
        if not path.is_file():
            continue
        records = json.loads(path.read_text())
        if not isinstance(records, list):
            records = [records]
        if doctype == "Builder Page" and not templates:
            # Builder ships ~85 template pages with the app. They are upstream's
            # content, they are already present in any Builder instance, and
            # copying them makes every count meaningless. The site's own pages
            # are the ones that matter.
            records = [r for r in records if not r.get("is_template")]
        order.append((doctype, records))

    home = ""
    settings = doctypes / "website-settings.json"
    if settings.is_file():
        data = json.loads(settings.read_text())
        home = (data if isinstance(data, dict) else {}).get("home_page") or ""
    return order, home


def _run(job: dict) -> dict:
    """Send the script and its job into the container and read the report back."""
    payload = json.dumps(job)
    staged = "/tmp/buildsmith-adopt.json"

    write = subprocess.run(
        [*COMPOSE, "exec", "-T", "bench", "bash", "-lc", f"cat > {staged}"],
        input=payload, text=True, capture_output=True,
    )
    if write.returncode != 0:
        raise CouldNotCheck(f"could not stage the job: {write.stderr.strip()}")

    script = SCRIPT.read_text().replace(
        "job = json.loads(sys.stdin.read())",
        f"job = json.loads(open({staged!r}).read())",
    )
    completed = subprocess.run(
        [*COMPOSE, "exec", "-T", "bench", "bash", "-lc",
         f"cd {BENCH}/sites && {BENCH}/env/bin/python -"],
        input=script, text=True, capture_output=True,
    )
    subprocess.run([*COMPOSE, "exec", "-T", "bench", "rm", "-f", staged],
                   capture_output=True)

    line = next((ln for ln in completed.stdout.splitlines() if ln.startswith("ADOPT:")), None)
    if line is None:
        print(completed.stdout[-3000:])
        print(completed.stderr[-3000:], file=sys.stderr)
        raise CouldNotCheck("the dev instance did not report a result")
    return json.loads(line[len("ADOPT:"):])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="buildsmith adopt", description=__doc__.split("\n")[0])
    parser.add_argument("--site", required=True, help="site under sites/")
    parser.add_argument("--target", default="sandbox.localhost")
    parser.add_argument("--prune", action="store_true",
                        help="delete other published pages that collide on a route")
    parser.add_argument("--templates", action="store_true",
                        help="also copy Builder's ~85 shipped template pages")
    args = parser.parse_args(argv)

    _refuse_non_local(args.target)
    export_dir = ROOT / "sites" / args.site / "live-export"
    order, home = load_export(export_dir, templates=args.templates)
    if not order:
        raise CouldNotCheck(f"{export_dir}/doctypes holds no Builder records")

    print(f"adopting '{args.site}' into {args.target}, in dependency order:")
    for doctype, records in order:
        print(f"  {doctype}: {len(records)}")

    report = _run({"site": args.target, "home_page": home, "prune": args.prune,
                   "order": [[d, r] for d, r in order]})

    print()
    problems = 0
    for step in report["steps"]:
        line = (f"  {step['doctype']}: {step['inserted']} inserted, "
                f"{step['updated']} updated")
        if step["failed"]:
            line += f", {step['failed']} FAILED"
            problems += step["failed"]
        print(line)
        for failure in step["failures"][:5]:
            print(f"      {failure['name']}: {failure['error']}")

    if report.get("home_page"):
        print(f"  Website Settings.home_page = {report['home_page']}")

    collisions = report.get("collisions") or []
    if collisions:
        verb = "removed" if report.get("pruned") else "STILL PUBLISHED"
        print(f"\n  route collisions — {len(collisions)} ({verb})")
        for c in collisions[:8]:
            print(f"      /{c['route']} is also served by {c['name']}")
        if not report.get("pruned"):
            print("  Two published pages on one route do not error: Builder resolves with\n"
                  "  `published_at desc, creation desc`, so one silently wins and the other\n"
                  "  becomes unreachable (TRAP-010). Re-run with --prune to remove them.")
            problems += len(collisions)

    # The guarantee this path exists for. Stated as a count, because a
    # var(--uuid) pointing at a variable that does not exist is invisible: the
    # page renders on its literal fallback and looks almost right (ADR-005).
    print(f"\n  Builder Variables present: {report['variables_present']} "
          f"(expected at least {report['variables_expected']})")
    if report["variables_present"] < report["variables_expected"]:
        print("  WARNING: fewer variables than exported — var(--uuid) references will\n"
              "  fall back to their literals and the page will look almost right.")
        problems += 1

    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
