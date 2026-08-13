"""Publication guard for this repo.

Buildsmith is intended to be published. Two things must never reach a tracked
file, a commit message, a path, or a branch name: client-identifying tokens and
private network data. Neither check is reimplemented here.

  1. Tokens + RFC1918     delegated to OpsKit's bin/publication-guard.sh, so the
                          client-token list stays single and anchored to OpsKit
                          (ROADMAP section 3).
  2. Site isolation       done locally, because it is about *our* layout: a
                          neutrally-named sites/<site>/ path trips no token, so
                          only Buildsmith can catch it.
  3. Branch names         done locally, for the same reason, against the same
                          token list.

OpsKit's guard resolves both the tree under test and its token sources from
OPSKIT_ROOT. Until it grows --repo (OpsKit issue, ROADMAP section 3), we call it
with OPSKIT_ROOT pointed at *this* repo and feed the token list in through
CLIENT_TOKENS, which it already supports.

FAIL CLOSED. If OpsKit cannot be located, or its token list comes back empty,
the delegated check would silently pass — the token loop simply never runs. That
is the dangerous failure mode this module exists to prevent, so both conditions
are hard errors unless BUILDSMITH_PUBLIC_ONLY=1 (CI on the public repo, where
there are no client tokens to leak).

The delegated guard is invoked as a subprocess and stays a shell script on
purpose: it belongs to OpsKit. Porting it here would fork the token logic, which
is the one thing this design exists to avoid.

Usage:
    buildsmith guard --cached                 staged changes (pre-commit)
    buildsmith guard <base>...<head>          a diff range (CI)
    buildsmith guard --messages <range>       commit messages of a range
    buildsmith guard --message-file <path>    one commit message (commit-msg)

Environment:
    OPSKIT_ROOT              where OpsKit lives (default ~/Projects/opskit)
    CLIENT_TOKENS            extra tokens, merged with OpsKit's list
    BUILDSMITH_PUBLIC_ONLY=1 run without OpsKit; only explicit CLIENT_TOKENS apply

Overrides, for reviewed false positives only — never to force a file through:
    ALLOW_PRIVATE_IPS=1   ALLOW_CLIENT_TOKENS=1   ALLOW_SITE_PATHS=1
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from buildsmith.errors import EXIT_OK, EXIT_PROBLEM
from buildsmith.tools.gitenv import run_git

REPO_ROOT = Path(__file__).resolve().parents[2]

# A path that does not exist, used to ask collect_tokens() for the environment's
# explicit tokens alone. Naming it beats repeating a magic string.
NO_OPSKIT = Path("/nonexistent")


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "0") == "1"


def _git(*args: str) -> str:
    """Run git in the repo root and return stdout, or "" if it failed.

    Failure is not distinguished from empty output on purpose: every caller here
    is asking "which paths changed", and both answers mean "no paths to check".

    hermetic=False deliberately (see gitenv): this runs under pre-commit,
    where GIT_INDEX_FILE is load-bearing — a partial commit stages through a
    temporary index, and scrubbing the variable would make the guard check
    the wrong staged set. Same for the delegated OpsKit guard below.
    """
    proc = run_git(*args, cwd=REPO_ROOT, hermetic=False)
    return proc.stdout if proc.returncode == 0 else ""


def collect_tokens(opskit: Path) -> list[str]:
    """The client-token list, assembled the way OpsKit assembles it.

    Mirrors OpsKit's collect_tokens(), but reads it out of the OpsKit root rather
    than the repo under test: environments/* directory names (minus example), the
    gitignored .client-tokens file, and any CLIENT_TOKENS already exported.

    The list is never written to disk and never printed — Buildsmith is public,
    and an error message enumerating the tokens would leak precisely what the
    guard protects.
    """
    tokens: list[str] = []

    environments = opskit / "environments"
    if environments.is_dir():
        tokens += [
            d.name
            for d in environments.iterdir()
            if d.is_dir() and d.name != "example" and not d.name.startswith(".")
        ]

    token_file = opskit / ".client-tokens"
    if token_file.is_file():
        tokens += [
            line.strip()
            for line in token_file.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]

    extra = os.environ.get("CLIENT_TOKENS", "")
    if extra:
        tokens += re.split(r"[,\s]+", extra)

    return sorted({t for t in (t.strip() for t in tokens) if t})


def opskit_guard_path(opskit: Path) -> Path:
    return opskit / "bin" / "publication-guard.sh"


def opskit_root() -> Path:
    return Path(os.environ.get("OPSKIT_ROOT") or Path.home() / "Projects" / "opskit")


def _run_delegated(guard: Path, tokens: list[str], args: list[str]) -> int:
    env = dict(os.environ)
    env["CLIENT_TOKENS"] = "\n".join(tokens)
    # OPSKIT_ROOT here names the tree under test — us, not OpsKit.
    env["OPSKIT_ROOT"] = str(REPO_ROOT)
    return subprocess.run(
        ["bash", str(guard), *args], cwd=REPO_ROOT, env=env, check=False
    ).returncode


def run_opskit_guard(args: list[str]) -> int:
    """Tokens and private addresses, checked by OpsKit. Fails closed."""
    opskit = opskit_root()
    guard = opskit_guard_path(opskit)

    if _env_flag("BUILDSMITH_PUBLIC_ONLY"):
        # No client data to protect, so OpsKit is not required. If its guard
        # happens to be reachable we still run it — that keeps the RFC1918 check
        # alive — but only with tokens supplied explicitly. Nothing is
        # reimplemented locally when it is absent (ROADMAP section 3); we say so
        # loudly instead, because a quiet skip is exactly the failure this file
        # exists to prevent.
        if guard.is_file():
            print("NOTE: BUILDSMITH_PUBLIC_ONLY=1 — running guard with explicit tokens only.")
            return _run_delegated(guard, collect_tokens(NO_OPSKIT), args)
        print(f"WARNING: BUILDSMITH_PUBLIC_ONLY=1 and no OpsKit guard at '{guard}'.")
        print("         Token and private-IP checks did NOT run. Site isolation still applies.")
        return EXIT_OK

    if not opskit.is_dir():
        print(
            f"ERROR: OpsKit not found at '{opskit}'.\n"
            "The publication guard cannot run, so this commit is refused (fail closed).\n"
            "Set OPSKIT_ROOT, or use BUILDSMITH_PUBLIC_ONLY=1 if this tree genuinely has no\n"
            "client data to protect.",
            file=sys.stderr,
        )
        return EXIT_PROBLEM

    if not guard.is_file():
        print(
            f"ERROR: OpsKit guard missing: {guard}\n"
            "Refusing to commit (fail closed) — see AGENTS.md 'Publication guard'.",
            file=sys.stderr,
        )
        return EXIT_PROBLEM

    tokens = collect_tokens(opskit)
    if not tokens:
        print(
            "ERROR: OpsKit's client-token list is empty.\n"
            "An empty list makes the delegated token check a no-op, which is indistinguishable\n"
            f"from passing. Refusing to commit (fail closed). Check {opskit}/.client-tokens and\n"
            f"{opskit}/environments/.",
            file=sys.stderr,
        )
        return EXIT_PROBLEM

    return _run_delegated(guard, tokens, args)


def check_branch_name() -> int:
    """A branch name is published the moment it is pushed.

    It persists in the remote ref list and in PR titles long after the branch is
    deleted locally. The delegated guard never sees branch names, and a
    client-suggestive branch had already reached a public origin in a
    neighbouring repo before this check existed (BS-002).
    """
    if _env_flag("ALLOW_CLIENT_TOKENS"):
        return EXIT_OK

    # symbolic-ref, not rev-parse: `rev-parse --abbrev-ref HEAD` fails on an
    # unborn branch, which would silently skip this check on the very first
    # commit — exactly when a fresh client-named branch is most likely to exist.
    # Detached HEAD has no branch name, so symbolic-ref failing there is correct.
    branch = _git("symbolic-ref", "--short", "HEAD").strip()
    if not branch:
        return EXIT_OK

    opskit = NO_OPSKIT if _env_flag("BUILDSMITH_PUBLIC_ONLY") else opskit_root()

    for token in collect_tokens(opskit):
        # Anchored to word boundaries, matching OpsKit's own
        # `grep -qiE "\b${tok}\b"` (bin/publication-guard.sh --branch) — a short
        # token (e.g. a 2-letter environment abbreviation) is otherwise a bare
        # substring match away from firing on an ordinary word that merely
        # contains it (issue #50).
        #
        # The tokens are patterns, as OpsKit's guard treats them. A malformed
        # one must not crash the guard into passing, so it falls back to a
        # literal match. Compiling the token by itself, before wrapping it in
        # `\b...\b`, matters: a token ending in a lone backslash is invalid
        # regex on its own, but interpolating it directly into `\b{token}\b`
        # lets the boundary's own `\b` complete the escape into something
        # that compiles — silently matching the wrong thing instead of
        # falling back.
        try:
            re.compile(token)
            pattern = token
        except re.error:
            pattern = re.escape(token)
        hit = re.search(rf"\b{pattern}\b", branch, re.IGNORECASE)
        if hit:
            # The token itself is never echoed — this output is read in a
            # terminal that may be shared, and the tokens are the secret.
            print(f"ERROR: the branch name '{branch}' contains a client token.")
            print("Branch names are published as soon as they are pushed, and they outlive")
            print("the branch in the remote ref list and in PR titles. Rename it:")
            print("  git branch -m <neutral-name>")
            return EXIT_PROBLEM
    return EXIT_OK


def check_site_isolation(mode: str) -> int:
    """Only sites/example/ is publishable.

    Everything else under sites/ is the private layer; .gitignore covers the
    accident, this covers the `git add -f`.
    """
    if _env_flag("ALLOW_SITE_PATHS"):
        return EXIT_OK

    if mode == "--cached":
        out = _git("diff", "--cached", "--name-only", "--diff-filter=ACM")
    else:
        out = _git("diff", mode, "--name-only", "--diff-filter=ACM")

    offenders = [
        p
        for p in out.splitlines()
        if p.startswith("sites/") and not p.startswith("sites/example/")
    ]
    if offenders:
        print("ERROR: Private site files cannot be committed:")
        for p in offenders:
            print(p)
        print("Only sites/example/ is tracked — sites/<site>/ is the private layer")
        print("(AGENTS.md 'The private layer'). Unstage these; do not add -f.")
        return EXIT_PROBLEM
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:]) or ["--cached"]
    mode = args[0]

    if mode in ("--messages", "--message-file"):
        return run_opskit_guard(args)

    # Every check runs even when an earlier one fails, so one commit attempt
    # surfaces every problem rather than one per fix-and-retry cycle.
    results = [
        run_opskit_guard(args),
        check_site_isolation(mode),
        check_branch_name(),
    ]
    return EXIT_PROBLEM if any(results) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
