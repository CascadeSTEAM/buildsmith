"""Everything the pre-commit hook checks, so the hook itself stays two lines.

Kept separate from `guard` because these are different kinds of check. The guard
protects *publication* — nothing client-identifying may leave. This adds the
checks that protect *correctness* of generated artifacts, which are only run
when something that feeds them is staged.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from buildsmith.errors import EXIT_OK, EXIT_PROBLEM
from buildsmith.tools import guard

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Staged paths ruff lints. Only our Python — vendored or generated content
#: never gates a commit on style.
LINTABLE = re.compile(r"^(buildsmith/|tests/).*\.py$|^install\.py$")

# Staged paths that make docs/catalog.md potentially stale. Unrelated commits
# skip the regeneration check entirely, so this stays cheap.
FEEDS_CATALOG = re.compile(
    r"^(buildsmith/primitives/|buildsmith/workflows/|buildsmith/tools/docgen\.py"
    r"|sites/example/design/)"
)


def staged_paths() -> list[str]:
    proc = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.splitlines()


def main(argv: list[str] | None = None) -> int:
    code = guard.main(["--cached"])
    if code != EXIT_OK:
        return code

    # Generic credentials (cloud keys, API tokens, private-key blocks) — a
    # class the guard has never heard of. Runs on every commit, not path-
    # gated: any staged file can hold a secret. Fails closed at exit 2 when
    # gitleaks is absent (ADR-010).
    from buildsmith.tools import secretscan

    code = secretscan.main([])
    if code != EXIT_OK:
        return code

    # Discouraged, and never a way to get a client-identifying file through —
    # the guard above has already run and cannot be skipped from here. This
    # covers trees that have no generated docs to be stale, such as the
    # publication-guard test fixture.
    if os.environ.get("ALLOW_STALE_DOCS") == "1":
        print("NOTE: ALLOW_STALE_DOCS=1 — generated-doc checks skipped.")
        print("All checks passed.")
        return EXIT_OK

    from buildsmith.tools import docgen, schemagen

    # Generated docs cannot rot: if the primitives, a workflow or the example
    # site's design inputs changed, docs/catalog.md must have been regenerated.
    if any(FEEDS_CATALOG.match(p) for p in staged_paths()):
        code = max(code, docgen.main(["--check"]))

    # Cheap enough to run unconditionally: compares the commit recorded in
    # docs/builder-schema.md against the pin. Moving the pin without
    # regenerating means the schema reference describes a Builder we no longer
    # target — and the commit that moves the pin is exactly the one that would
    # skip a staged-path check.
    code = max(code, schemagen.main(["--check"]))

    # Lint the staged Python. A configured linter nothing executes is the
    # same defect as a stale generated doc — so it runs here, and a missing
    # ruff FAILS rather than skipping: a check that cannot run must never
    # look like it passed. (install.py --dev provides it via the dev extra.)
    lintable = [p for p in staged_paths() if LINTABLE.match(p)]
    if lintable:
        from buildsmith.tools.prepush import dev_python

        ruff = subprocess.run(
            [dev_python(), "-m", "ruff", "check", *lintable],
            cwd=REPO_ROOT,
        )
        if ruff.returncode != 0:
            print(
                "ruff failed (or is not installed — `pip install -e '.[dev]'`).",
                file=sys.stderr,
            )
            code = max(code, EXIT_PROBLEM)
        else:
            print(f"OK: ruff clean over {len(lintable)} staged file(s)")

    if code == EXIT_OK:
        print("All checks passed.")
    return code


if __name__ == "__main__":
    sys.exit(main())
