"""Generic credential scanning, delegated to gitleaks.

The publication guard and the audit protect against *our* failure mode:
client-identifying facts reaching a public repo. Neither of them knows what an
AWS key, a Stripe token or a `BEGIN PRIVATE KEY` block looks like — that is a
different, well-solved problem, and reimplementing gitleaks' several hundred
curated rules here would be the same fork-the-token-list mistake ADR-010 exists
to prevent. So gitleaks runs alongside the guard, never instead of it.

Two entry points, matching the two boundaries the hooks already defend:

    buildsmith secretscan            staged changes (pre-commit)
    buildsmith secretscan --history  every commit ever made (pre-push, CI)

FAIL CLOSED, exit 2 not 1. A missing gitleaks binary means the check never ran,
which must never read as "no secrets found". The distinction matters to anyone
scripting on exit codes: 1 says "go find the leak", 2 says "go install the
tool".

Findings are printed redacted. The scan output may land in a shared terminal or
a public CI log, and echoing the secret there would finish the leak the scan
exists to stop.

Escape hatch: BUILDSMITH_SKIP_GITLEAKS=1, for a machine that genuinely cannot
run the binary. A reviewed false positive is different — that belongs in
`.gitleaks.toml`'s allowlist, in a commit, where the review is visible.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from buildsmith.errors import EXIT_OK, EXIT_PROBLEM, EXIT_UNCHECKED
from buildsmith.tools.gitenv import hermetic_env

REPO_ROOT = Path(__file__).resolve().parents[2]

INSTALL_HINT = (
    "gitleaks is not installed (or not on PATH).\n"
    "Get it from https://github.com/gitleaks/gitleaks/releases — one static\n"
    "binary, no dependencies. Then re-run. A scanner that cannot run must\n"
    "never look like it passed, so this refuses (exit 2) rather than skipping.\n"
    "A machine that truly cannot run it: BUILDSMITH_SKIP_GITLEAKS=1 (reviewed\n"
    "use only — the commit still reaches a public repo unscanned)."
)


def gitleaks_path() -> str | None:
    return shutil.which("gitleaks")


def run_gitleaks(scan_args: list[str], *, hermetic: bool = True) -> int:
    """Run gitleaks in the repo root and map its exit into our contract.

    gitleaks exits 0 clean and 1 on findings, which happens to match this
    repo's 0/1. Anything else (bad config, not a git repo) is a scan that
    never completed — exit 2, not 1, so nobody hunts for a leak that was
    never actually detected.

    hermetic=True (the default, for scan_history): gitleaks reads the
    repository through git, and an inherited GIT_DIR would out-rank cwd (see
    gitenv) — REPO_ROOT should decide. scan_staged passes hermetic=False:
    under pre-commit, git hands gitleaks' internal `git diff --staged` a
    temporary GIT_INDEX_FILE for a partial commit, and scrubbing it would
    make gitleaks scan the wrong staged set — the same reasoning guard.py's
    _git documents for its own pre-commit path.
    """
    binary = gitleaks_path()
    if binary is None:
        print(INSTALL_HINT, file=sys.stderr)
        return EXIT_UNCHECKED

    env = hermetic_env() if hermetic else dict(os.environ)
    proc = subprocess.run(
        [binary, *scan_args, "--redact", "--config", ".gitleaks.toml"],
        cwd=REPO_ROOT,
        check=False,
        env=env,
    )
    if proc.returncode == 0:
        return EXIT_OK
    if proc.returncode == 1:
        print(
            "\nSecret(s) detected above (redacted). If one is real: remove it,\n"
            "rotate the credential — it is compromised the moment it is in a\n"
            "commit — and only then re-commit. If it is a reviewed false\n"
            "positive, allowlist it in .gitleaks.toml so the review is visible\n"
            "in the diff. Never push past this with --no-verify.",
            file=sys.stderr,
        )
        return EXIT_PROBLEM
    print(f"gitleaks itself failed (exit {proc.returncode}) — the scan never ran.",
          file=sys.stderr)
    return EXIT_UNCHECKED


def scan_staged() -> int:
    """The staged diff, on its way into a commit."""
    return run_gitleaks(["git", "--pre-commit", "--staged"], hermetic=False)


def scan_history() -> int:
    """Every commit ever made, before any of them leaves the machine.

    Same reasoning as the audit in prepush.py: a secret that slipped into
    history three commits ago passes every staged-diff scan forever after,
    and history is exactly what gets read once a repo is public.

    --all, not the current branch: a bare `gitleaks git` walks only HEAD's
    ancestry, and the branch being pushed is not always the branch checked
    out. The audit scans all refs for the same reason.
    """
    return run_gitleaks(["git", "--log-opts=--all"])


def main(argv: list[str] | None = None) -> int:
    # argparse rather than an `in` test: a mistyped `--histroy` must error
    # out, not silently run the staged scan and report history clean.
    parser = argparse.ArgumentParser(prog="buildsmith secretscan")
    parser.add_argument("--history", action="store_true",
                        help="scan every commit on every ref, not the staged diff")
    args = parser.parse_args(argv)

    if os.environ.get("BUILDSMITH_SKIP_GITLEAKS") == "1":
        print("NOTE: BUILDSMITH_SKIP_GITLEAKS=1 — generic secret scan did NOT run.")
        return EXIT_OK

    return scan_history() if args.history else scan_staged()


if __name__ == "__main__":
    sys.exit(main())
