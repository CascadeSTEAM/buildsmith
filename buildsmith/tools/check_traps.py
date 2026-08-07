"""Prove the sandbox reproduces known traps — the faithfulness check.

A sandbox that merely *runs* proves nothing. This proves it fails the way
production fails, on the traps whose failure modes are entirely silent.

The script that does the checking runs **inside** the bench container, because
it imports Builder. It lives beside this file as data rather than being embedded
as a string: a script-in-a-string needs escaping, and escaping is where a check
quietly stops checking what you think it checks.
"""

from __future__ import annotations

import sys
from pathlib import Path

from buildsmith.tools.sandbox import load_pins, require_running, run_bench

SCRIPT = Path(__file__).parent / "bench_scripts" / "trap_check.py"

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    require_running()
    pins = load_pins()
    print(f"Builder pin: {pins.builder} ({pins.get('BUILDER_REF_STATUS')})\n")
    try:
        out = run_bench(SCRIPT.read_text())
    except Exception as exc:  # noqa: BLE001 - the bench reports its own detail
        print(exc, file=sys.stderr)
        return 1
    sys.stdout.write(out)
    return 1 if "NOT FAITHFUL" in out else 0


if __name__ == "__main__":
    raise SystemExit(main())
