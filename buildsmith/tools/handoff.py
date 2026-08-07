"""The seam between emitting payloads and applying them.

This module does NOT apply anything, and it never will. Buildsmith owns design
artifacts; the operations project owns actions against live systems (ADR-002).
What this does is resolve where that project is, validate what we are about to
hand over, and print the brief whoever applies it needs.

Buildsmith must stay usable by someone with no operations project at all — a
demo site, no client, no infrastructure — so every entry point degrades to
something useful rather than failing outright. Only the brief needs the other
project, and only to name it in the output.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from buildsmith.errors import EXIT_OK, EXIT_PROBLEM
from buildsmith.tools import validate


def opskit_root() -> Path | None:
    """Where the operations project is, or None if there isn't one."""
    candidate = Path(os.environ.get("OPSKIT_ROOT") or Path.home() / "Projects" / "opskit")
    return candidate if candidate.is_dir() else None


def active_env() -> str | None:
    """The selected operations environment, if one is selected."""
    root = opskit_root()
    if root is None:
        return None
    env_file = root / ".env"
    if not env_file.is_file():
        return None
    for line in env_file.read_text().splitlines():
        if line.startswith("ACTIVE_ENV="):
            return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None


def prerequisites(payload_dir: Path) -> list[str]:
    """What the TARGET site must already have for these payloads to apply.

    Prerequisites are about the target, so validating the files cannot surface
    them. Applying a page before its project folder exists fails with
    LinkValidationError; publishing a home page without setting
    Website Settings.home_page leaves / serving the desk login.
    """
    folders: set[str] = set()
    home = False

    for path in sorted(payload_dir.rglob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict) or record.get("doctype") != "Builder Page":
            continue
        if record.get("project_folder"):
            folders.add(record["project_folder"])
        if record.get("route") == "home":
            home = True

    lines = [f"  - a `Builder Project Folder` named {f!r} must already exist"
             for f in sorted(folders)]
    if home:
        lines.append("  - `Website Settings.home_page` must be set to 'home'")
    return lines


def brief(payload_dir: Path) -> int:
    """Validate a payload directory and print the operations handoff brief."""
    if not payload_dir.is_dir():
        print(f"ERROR: {payload_dir} is not a directory", file=sys.stderr)
        return EXIT_PROBLEM

    # Never hand over a payload nobody checked. This is the whole reason this
    # entry point exists rather than someone copying files by hand.
    code = validate.main(["--dir", str(payload_dir)])
    if code != EXIT_OK:
        return code

    payloads = sorted(payload_dir.rglob("*.json"))

    print("\n=== handoff brief ===\n")
    print(f"Payloads in: {payload_dir}")
    for p in payloads:
        print(f"  {p}")
    print()
    print("These are FILES. Applying them is an action against a live system, so it is")
    print("performed by a subagent in the operations project — never from this repo.")
    print()

    root = opskit_root()
    if root is not None:
        print(f"  operations project: {root}")
        env_name = active_env()
        if env_name:
            print(f"  active environment: {env_name}")
        print("  the subagent should load its 'frappe-access' skill and take the path it")
        print("  dictates — Path A (API) by default, Path B only where admin is genuinely")
        print("  required.")
    else:
        print("  No operations project found. Apply these by whatever route you use;")
        print("  nothing about the payloads depends on it.")
    print()

    prereqs = prerequisites(payload_dir)
    if prereqs:
        print("The target site must already have:")
        print("\n".join(prereqs))
        print()

    print("Before applying:")
    print("  - snapshot / back up first. A Builder Snapshot is the cheap safety net.")
    print("  - a component payload needs a clean `buildsmith simulate` run against a current")
    print("    state export, or it can collapse every page using it (TRAP-001).")
    print("  - a template page with a template_group needs developer_mode ON, and saving")
    print("    it writes fixture files onto the server (TRAP-006).")
    print("  - read the token map back afterwards; composition depends on it being current.")
    print()
    print("Immediately after applying:")
    print("  - clear the website cache: bench --site <site> clear-website-cache.")
    print("    find_page_with_path is redis-cached for an hour, so a replaced page leaves")
    print("    its route pointing at a deleted docname and every visitor gets a 403")
    print("    until the cache expires (TRAP-015).")
    print("  - browse every route as an anonymous visitor, not just as admin.")
    print()
    print("Then: record the run with `buildsmith journal append`.")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="buildsmith handoff", description=__doc__)
    sub = parser.add_subparsers(dest="what")
    sub.add_parser("root", help="where the operations project is, if anywhere")
    sub.add_parser("env", help="the active environment, if one is selected")
    p = sub.add_parser("brief", help="validate a payload dir and print the brief")
    p.add_argument("dir", type=Path)
    args = parser.parse_args(argv)

    if args.what == "root":
        root = opskit_root()
        if root is None:
            print(
                "no operations project found (looked for ${OPSKIT_ROOT:-~/Projects/opskit}).\n"
                "That is fine for a standalone site; only the handoff brief needs it.",
                file=sys.stderr,
            )
            return EXIT_PROBLEM
        print(root)
        return EXIT_OK

    if args.what == "env":
        name = active_env()
        if not name:
            print("no active environment selected.", file=sys.stderr)
            return EXIT_PROBLEM
        print(name)
        return EXIT_OK

    if args.what == "brief":
        return brief(args.dir)

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
