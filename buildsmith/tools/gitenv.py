"""A subprocess environment that cannot reach the caller's repository.

git exports its repository location to every hook it runs: GIT_DIR,
GIT_INDEX_FILE, GIT_WORK_TREE and family. Those variables override both
`git -C <path>` and `cwd=` — a location in the environment beats a location
on the command line. Anything spawned from inside a hook inherits them, and
so does anything *that* spawns.

The failure that earned this module (#23): the first `git push` ever made
from a linked worktree — where GIT_DIR is an absolute path, not the relative
`.git` that dissolves harmlessly in a tempdir — had the pre-push hook's unit
suite drive every fixture `git init`/`commit`/`checkout` into the pusher's
real repository: poison fixtures committed onto the branch being pushed, the
worktree switched to a fake-client branch name, the shared config rewritten.

So: a subprocess whose target repository is stated explicitly (`-C ROOT`,
`cwd=REPO_ROOT`, a tempdir fixture) runs with hermetic_env(), making the
stated target the *only* one in reach. The exception is deliberate: the
pre-commit guard inherits GIT_* untouched, because GIT_INDEX_FILE is load-
bearing there — a partial commit stages through a temporary index, and
scrubbing it would make the guard check the wrong staged set.
"""

from __future__ import annotations

import os


def hermetic_env(**overrides: str) -> dict[str, str]:
    """A copy of os.environ with every GIT_* variable removed."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(overrides)
    return env
