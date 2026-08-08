"""The whole-repository audit, run before anything leaves the machine (BS-004).

`buildsmith guard` is a gate on the staged diff. It cannot see what is already
in the tree, and it cannot see history at all — so a leak that slipped through
once passes cleanly forever afterwards, and history is exactly what gets read
when a repository is made public.

`buildsmith audit` closes that, but until now nothing ran it automatically. It
is deliberately **not** in pre-commit: scanning full history on every commit is
slow, and a slow pre-commit hook is a hook someone disables — at which point the
fast checks stop running too.

Push is the right boundary. A leak that never left the machine can be fixed by
rewriting history. One that reached a remote cannot: it is in someone's clone,
in the forge's object store, and possibly in a cache that outlives deletion.

Refusing a push is disruptive, so this refuses only on findings — an audit that
cannot resolve a token list reports that loudly and fails, because a token check
with an empty list proves nothing while looking exactly like success.

Escape hatch: `BUILDSMITH_SKIP_AUDIT=1 git push`. For a findings-reviewed push
only. It is printed in the refusal message on purpose — an escape hatch nobody
can find gets replaced by `--no-verify`, which skips *every* hook including the
ones that would have caught the next problem.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from buildsmith.errors import EXIT_OK, EXIT_PROBLEM
from buildsmith.tools.gitenv import hermetic_env

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Set in the child suite's environment. The suite's own PrePushTest calls
#: main(), which without this marker would spawn the suite again, forever.
#: The tests stub run_suite() the way they stub audit.main; the marker is the
#: structural backstop for any caller that forgets.
_IN_SUITE = "BUILDSMITH_PREPUSH_IN_SUITE"


def dev_python() -> str:
    """The interpreter that carries the dev extra.

    Git spawns hooks under whatever python3 the two-line shim finds — on a
    workstation that is the system interpreter, which has neither ruff nor
    any reason to. `install.py --dev` puts the tools in `.venv`; prefer it
    when it exists so the hook tests what the developer actually runs.
    """
    venv = REPO_ROOT / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def run_suite() -> int:
    """Run the unit suite in a child interpreter and return its exit code.

    No `GIT_*` variable survives into the child (see gitenv). GIT_DIR
    overrides the `git -C <tempdir>` every test fixture relies on; from a
    linked worktree it is absolute, and the suite's fixtures once committed
    their poison data onto the very branch being pushed (#23). The suite
    must reach repositories only through paths it built itself.
    """
    env = hermetic_env(**{_IN_SUITE: "1"})
    return subprocess.run(
        [dev_python(), "-m", "unittest", "discover", "-s", "tests", "-q"],
        cwd=REPO_ROOT,
        env=env,
    ).returncode


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("BUILDSMITH_SKIP_AUDIT") == "1":
        print(
            "NOTE: BUILDSMITH_SKIP_AUDIT=1 — the unit suite, publication "
            "audit and secret scan did not run."
        )
        return EXIT_OK

    print("=== buildsmith pre-push: unit suite + publication audit + secret scan ===")

    # The suite runs in under two seconds; nothing else guarantees a pushed
    # tree is green, and "green by discipline" is a streak, not a property.
    if os.environ.get(_IN_SUITE) == "1":
        print("NOTE: already inside the suite — not spawning it again.")
        suite_code = EXIT_OK
    else:
        suite_code = run_suite()
    if suite_code != 0:
        print(
            "\nPUSH REFUSED — the unit suite is red. Fix it (or, for a "
            "reviewed emergency,\nBUILDSMITH_SKIP_AUDIT=1 skips this too).",
            file=sys.stderr,
        )
        return EXIT_PROBLEM

    # Full-history secret scan (ADR-010) before the audit: gitleaks covers the
    # class of leak — cloud keys, API tokens, private-key blocks — that the
    # audit's client-fact patterns were never written to see. Same boundary
    # logic as the audit itself: a secret that slipped into history passes
    # every staged-diff scan forever after, and push is the last moment it is
    # still recoverable by rewriting.
    from buildsmith.tools import audit, secretscan

    scan_code = secretscan.main(["--history"])
    if scan_code != EXIT_OK:
        print(
            "\nPUSH REFUSED — the secret scan found something (or could not "
            "run, which\nmust never pass silently). See above.",
            file=sys.stderr,
        )
        # The scan's own code, not a flattened 1: git refuses on any nonzero,
        # and a caller scripting on 1-vs-2 must still see "could not check".
        return scan_code

    code = audit.main(["--scope", "all"])
    if code == EXIT_OK:
        return EXIT_OK

    print(
        "\nPUSH REFUSED — the audit found something above.\n"
        "\n"
        "Read the findings; not every one is a leak. A finding you have reviewed\n"
        "and dismissed can be pushed past with:\n"
        "\n"
        "    BUILDSMITH_SKIP_AUDIT=1 git push\n"
        "\n"
        "Use that rather than `--no-verify`, which skips every hook including the\n"
        "ones that would catch the next thing. And never genericise by disabling a\n"
        "check — genericise the file.",
        file=sys.stderr,
    )
    return EXIT_PROBLEM


if __name__ == "__main__":
    sys.exit(main())
