"""Install this repo's git hooks.

`core.hooksPath` is per-clone local config, so a fresh clone has the publication
guard switched off until this runs. That is worth stating plainly: the guard is
not on by default, and a clone that has never run this will happily commit a
client's name.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

from buildsmith.tools.gitenv import run_git

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / ".githooks"

__all__ = ["install", "is_configured", "main"]


def _git(*args: str) -> str:
    return run_git("-C", str(ROOT), *args).stdout.strip()


def is_configured() -> tuple[bool, str]:
    """Compare resolved paths, not strings.

    `core.hooksPath` may legitimately be absolute or relative; comparing the raw
    string makes a perfectly valid absolute setting read as "guards inactive".
    """
    current = _git("config", "core.hooksPath")
    if not current:
        return False, ""
    candidate = Path(current) if Path(current).is_absolute() else ROOT / current
    try:
        return candidate.resolve() == HOOKS.resolve(), current
    except OSError:
        return False, current


def install() -> int:
    if not HOOKS.is_dir():
        print(f"ERROR: {HOOKS} not found.", file=sys.stderr)
        return 1

    configured, current = is_configured()
    if configured:
        print(f"OK: core.hooksPath already resolves to .githooks ({current})")
    else:
        _git("config", "core.hooksPath", ".githooks")
        print(f"OK: core.hooksPath -> .githooks (was {current or 'unset'!r})")

    # A hook without the executable bit is silently skipped by git — the same
    # failure mode as never configuring the path at all.
    for hook in sorted(HOOKS.iterdir()):
        if hook.is_file() and not os.access(hook, os.X_OK):
            hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            print(f"OK: chmod +x {hook.name}")

    print("OK: guards active: " + " ".join(h.name for h in sorted(HOOKS.iterdir())
                                           if h.is_file()))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true",
                        help="report only; non-zero if the guards are inactive")
    args = parser.parse_args(argv)

    if args.check:
        configured, current = is_configured()
        if configured:
            print(f"OK: core.hooksPath = {current}")
            return 0
        print(f"WARNING: core.hooksPath is {current or 'unset'!r} — the publication "
              "guard is INACTIVE.\n  Fix: buildsmith hooks", file=sys.stderr)
        return 1
    return install()


if __name__ == "__main__":
    raise SystemExit(main())
