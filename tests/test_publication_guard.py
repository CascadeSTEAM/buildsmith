"""The publication guard's exit criterion (M1 step 2), as a test.

A commit carrying (a) a client token, (b) a 10.x address, and (c) a staged
sites/<site>/ path must be rejected on ALL THREE counts. Guards are only worth
what their tests prove, so this also covers the two silent-pass modes that make
a guard worse than none: no OpsKit, and an empty token list.

Everything runs in a throwaway repo under a temp dir, against a throwaway OpsKit
root whose only token is the fictional 'acmecorp'. No real client token is ever
written to disk by this test — Buildsmith is public.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from buildsmith.tools.gitenv import hermetic_env, run_git

REPO_ROOT = Path(__file__).resolve().parents[1]

# Assembled from octets rather than written literally: a real RFC1918 address in
# a tracked file is exactly what the guard rejects, and this file is tracked.
POISON_IP = ".".join(str(o) for o in (10, 42, 7, 9))
POISON_TOKEN = "acmecorp"


def real_opskit_guard() -> Path:
    root = Path(os.environ.get("OPSKIT_ROOT") or Path.home() / "Projects" / "opskit")
    return root / "bin" / "publication-guard.sh"


class PublicationGuardTest(unittest.TestCase):
    """Exercises the guard end to end in a throwaway repo."""

    @classmethod
    def setUpClass(cls) -> None:
        # The delegated checks are OpsKit's, so a copy of its guard is what we
        # test against. Without one there is nothing to exercise; skip loudly
        # rather than reporting a pass we did not earn.
        cls.opskit_guard = real_opskit_guard()
        if not cls.opskit_guard.is_file():
            raise unittest.SkipTest(
                f"OpsKit guard not found at {cls.opskit_guard}. "
                "The publication-guard test needs it to exercise the delegated "
                "checks. Set OPSKIT_ROOT and re-run. (Nothing was verified.)"
            )

        cls._tmp = tempfile.TemporaryDirectory()
        work = Path(cls._tmp.name)

        # Two throwaway OpsKit roots: one with a fictional token, one with none.
        cls.fake_opskit = work / "opskit"
        cls.empty_opskit = work / "opskit-empty"
        for root in (cls.fake_opskit, cls.empty_opskit):
            (root / "bin").mkdir(parents=True)
            (root / "environments" / "example").mkdir(parents=True)
            shutil.copy(cls.opskit_guard, root / "bin" / "publication-guard.sh")
        (cls.fake_opskit / ".client-tokens").write_text(
            f"# fictional test client\n{POISON_TOKEN}\n"
        )
        cls.missing_opskit = work / "nope"

        # A throwaway repo holding the guard machinery and nothing else.
        cls.repo = work / "repo"
        cls.repo.mkdir()
        shutil.copytree(
            REPO_ROOT / "buildsmith",
            cls.repo / "buildsmith",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        shutil.copytree(REPO_ROOT / ".githooks", cls.repo / ".githooks")
        shutil.copy(REPO_ROOT / ".gitignore", cls.repo / ".gitignore")
        (cls.repo / "sites" / "example").mkdir(parents=True)

        cls._git("init", "-q", "-b", "main")
        cls._git("config", "user.name", "poison test")
        cls._git("config", "user.email", "poison@example.invalid")
        cls._git("config", "core.hooksPath", ".githooks")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    # --- helpers ------------------------------------------------------------

    @classmethod
    def _git(cls, *args: str) -> subprocess.CompletedProcess:
        return run_git("-C", str(cls.repo), *args)

    def _guard(self, *args: str, **overrides: str) -> subprocess.CompletedProcess:
        # The guard spawns git of its own; it must inherit no GIT_* either.
        env = hermetic_env()
        env["OPSKIT_ROOT"] = str(self.fake_opskit)
        # The fixture repo is a partial copy: the guard machinery and nothing
        # else. It has no design inputs and no pin, so the hook's generated-doc
        # checks have nothing to generate from. Scoped off here deliberately
        # rather than softened in the hook — this fixture exists to test the
        # publication guard, and the catalogue's own staleness check is covered
        # in tests/test_theme_build.py.
        env["ALLOW_STALE_DOCS"] = "1"
        env.update(overrides)
        return subprocess.run(
            ["python3", "-m", "buildsmith.tools.guard", *args],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def assertRejected(self, proc: subprocess.CompletedProcess, expect: str) -> None:
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, f"command succeeded, expected rejection\n{out}")
        self.assertIn(expect.lower(), out.lower(), f"rejected, but not for {expect!r}\n{out}")

    def _stage_poison(self) -> None:
        self._git("reset", "-q")
        (self.repo / "notes.md").write_text(f"The {POISON_TOKEN} rollout notes.\n")
        (self.repo / "inventory.yml").write_text(f"host: {POISON_IP}\n")
        (self.repo / "sites" / "clientsite").mkdir(exist_ok=True)
        (self.repo / "sites" / "clientsite" / "site.yml").write_text("target: staging\n")
        self._git("add", "notes.md", "inventory.yml")
        self._git("add", "-f", "sites/clientsite/site.yml")

    # --- the exit criterion -------------------------------------------------
    # Note these run on an unborn HEAD — the very first commit is exactly when a
    # guard is most likely to be untested, so that is where we test it.

    def test_a_client_token_rejected(self) -> None:
        self._stage_poison()
        # The delegated guard exits at its first violation and checks addresses
        # before tokens, so the address count is suppressed to reach this one.
        self.assertRejected(
            self._guard("--cached", ALLOW_PRIVATE_IPS="1"), f"client token '{POISON_TOKEN}'"
        )

    def test_b_private_address_rejected(self) -> None:
        self._stage_poison()
        self.assertRejected(self._guard("--cached"), "private (RFC1918) addresses")

    def test_c_staged_private_site_path_rejected(self) -> None:
        self._stage_poison()
        self.assertRejected(
            self._guard("--cached", ALLOW_PRIVATE_IPS="1", ALLOW_CLIENT_TOKENS="1"),
            "Private site files cannot be committed",
        )

    def test_all_three_counts_reported_together(self) -> None:
        """One commit attempt should surface every problem, not one per retry."""
        self._stage_poison()
        out = (lambda p: p.stdout + p.stderr)(self._guard("--cached"))
        self.assertIn("private (RFC1918) addresses".lower(), out.lower())
        # Site isolation is ours and runs even after the delegated guard fails.
        self.assertIn("Private site files cannot be committed".lower(), out.lower())

    def test_hook_blocks_the_commit(self) -> None:
        """The hook must refuse, not just the module it calls."""
        self._stage_poison()
        env_backup = os.environ.get("OPSKIT_ROOT")
        os.environ["OPSKIT_ROOT"] = str(self.fake_opskit)
        os.environ["ALLOW_STALE_DOCS"] = "1"
        try:
            proc = self._git("commit", "-q", "-m", "poison")
        finally:
            if env_backup is None:
                os.environ.pop("OPSKIT_ROOT", None)
            else:
                os.environ["OPSKIT_ROOT"] = env_backup
        self.assertRejected(proc, "ERROR")

    def test_token_free_private_site_path_still_rejected(self) -> None:
        """The check OpsKit structurally cannot do for us must stand alone."""
        self._git("reset", "-q")
        (self.repo / "sites" / "clientsite").mkdir(exist_ok=True)
        (self.repo / "sites" / "clientsite" / "site.yml").write_text("target: staging\n")
        self._git("add", "-f", "sites/clientsite/site.yml")
        self.assertRejected(self._guard("--cached"), "Private site files cannot be committed")

    # --- fail closed: a guard that cannot run must not pass -----------------

    def test_missing_opskit_refuses(self) -> None:
        self._git("reset", "-q")
        (self.repo / "notes.md").write_text(f"The {POISON_TOKEN} rollout notes.\n")
        self._git("add", "notes.md")
        self.assertRejected(
            self._guard("--cached", OPSKIT_ROOT=str(self.missing_opskit)), "OpsKit not found"
        )

    def test_empty_token_list_refuses(self) -> None:
        self._git("reset", "-q")
        (self.repo / "notes.md").write_text(f"The {POISON_TOKEN} rollout notes.\n")
        self._git("add", "notes.md")
        self.assertRejected(
            self._guard("--cached", OPSKIT_ROOT=str(self.empty_opskit)), "token list is empty"
        )

    # --- commit messages and branch names are published too -----------------

    def test_token_in_commit_message_rejected(self) -> None:
        self._git("reset", "-q")
        msg = self.repo / ".git" / "COMMIT_POISON"
        msg.write_text(f"{POISON_TOKEN} went live today\n")
        self.assertRejected(
            self._guard("--message-file", str(msg)), "contains the client token"
        )

    def test_token_in_branch_name_rejected(self) -> None:
        """BS-002. A branch reaches the remote the moment it is pushed."""
        self._git("reset", "-q")
        self._git("checkout", "-q", "-b", f"feat/{POISON_TOKEN}-rollout")
        try:
            (self.repo / "harmless.md").write_text("nothing sensitive\n")
            self._git("add", "harmless.md")
            self.assertRejected(self._guard("--cached"), "branch name")
        finally:
            # HEAD may still be unborn, so there is no `main` to check out —
            # rename back instead. (That the check fires at all on an unborn
            # branch is the point: the first commit is exactly when a fresh
            # client-named branch exists.)
            self._git("branch", "-q", "-m", "main")
            self._git("reset", "-q")
            (self.repo / "harmless.md").unlink(missing_ok=True)

    def test_the_fixture_repo_is_the_only_repo_in_reach(self) -> None:
        """Under a pre-push hook the environment carries GIT_DIR, which
        overrides `git -C` — from a linked worktree it is absolute, and this
        class's own fixtures once landed on the developer's real repository
        (#23). With a poisoned ambient GIT_DIR, the helper must still answer
        from the throwaway repo."""
        old = os.environ.get("GIT_DIR")
        os.environ["GIT_DIR"] = "/nonexistent/elsewhere/.git"
        try:
            proc = self._git("rev-parse", "--absolute-git-dir")
        finally:
            if old is None:
                os.environ.pop("GIT_DIR", None)
            else:
                os.environ["GIT_DIR"] = old
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            Path(proc.stdout.strip()), (self.repo / ".git").resolve(),
            "git answered for a repository other than the fixture",
        )

    # --- control ------------------------------------------------------------

    def test_z_clean_commit_accepted(self) -> None:
        """Without this the suite would pass just as happily if the guard
        rejected everything unconditionally."""
        self._git("reset", "-q")
        for stale in ("notes.md", "inventory.yml"):
            (self.repo / stale).unlink(missing_ok=True)
        shutil.rmtree(self.repo / "sites" / "clientsite", ignore_errors=True)
        (self.repo / "sites" / "example" / "site.yml").write_text(
            "site: example\ntarget: sandbox\n"
        )
        self._git("add", "-A")
        proc = self._guard("--cached")
        self.assertEqual(
            proc.returncode, 0, f"clean tree rejected\n{proc.stdout}{proc.stderr}"
        )


if __name__ == "__main__":
    unittest.main()
