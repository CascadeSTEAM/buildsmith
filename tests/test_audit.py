"""The audit's exemptions are where it can quietly stop working.

Every exemption is a hole by construction. The value of the audit is that a
human reads it, and a finding that fires on every commit trains people to skim
the section it lives in — so exemptions are necessary. What makes them safe is
that each one is narrow, and narrowness is not something you can see by reading
the regex.
"""

from __future__ import annotations

import unittest
from unittest import mock

from buildsmith.tools.audit import scan_text
from buildsmith.tools.gitenv import run_git
from tests.fixtures import (
    CLIENT_DOMAIN as CLIENT,
)
from tests.fixtures import (
    CLIENT_PHONE,
    CLIENT_STREET,
)
from tests.fixtures import (
    FORGE_EMAIL as FORGE,
)
from tests.fixtures import (
    VENDOR_EMAIL as VENDOR,
)


def findings(text: str) -> int:
    return len(scan_text(text, "(test)", []))


class ToolAttributionExemptionTest(unittest.TestCase):
    """The tool-attribution trailer is on every commit this agent makes."""

    def test_tool_trailers_are_exempt(self) -> None:
        for line in (
            f"Co-Authored-By: Claude Opus 5 (1M context) <{VENDOR}>",
            f"Signed-off-by: A Developer <{FORGE}>",
            f"co-authored-by: lowercase <{VENDOR}>",
        ):
            self.assertEqual(findings(line), 0, line)

    def test_a_person_at_a_client_still_fires(self) -> None:
        """The exemption must not become 'trailers are fine'.

        Naming an individual at a client is exactly what the audit is for, and
        putting that name in a trailer must not launder it.
        """
        self.assertGreater(findings(f"Co-Authored-By: A Person <a.person@{CLIENT}>"), 0)

    def test_lookalike_domain_still_fires(self) -> None:
        """A vendor address with a client domain appended must not pass."""
        self.assertGreater(
            findings(f"Co-Authored-By: X <{VENDOR}.{CLIENT}>"), 0
        )

    def test_extra_address_on_a_trailer_still_fires(self) -> None:
        """Appending a real address to an exempt trailer must not hide it."""
        self.assertGreater(
            findings(f"Reviewed-by: {VENDOR} and also bob@{CLIENT}"), 0
        )

    def test_the_same_address_outside_a_trailer_still_fires(self) -> None:
        """The exemption is anchored to the trailer form, not to the address."""
        self.assertGreater(findings(f"mail me at bob@{CLIENT} about the rollout"), 0)

    def test_client_tokens_are_never_exempt(self) -> None:
        """Exemptions skip the *fact* patterns only.

        A client token in an otherwise-exempt line must still be found — the
        token check runs before any line-level exemption, and that ordering is
        the thing this asserts.
        """
        line = f"Co-Authored-By: Claude Opus 5 (1M context) <{VENDOR}>"
        self.assertGreater(len(scan_text(line + " for acmecorp", "(test)", ["acmecorp"])), 0)
        # ...and even when the token sits inside the exempt trailer itself.
        self.assertGreater(
            len(scan_text(f"Co-Authored-By: acmecorp <{VENDOR}>", "(t)", ["acmecorp"])),
            0,
        )


class SafeValueTest(unittest.TestCase):
    """Documentation-reserved values must not be flagged, or the audit is noise."""

    def test_documentation_values_are_quiet(self) -> None:
        for line in (
            "see https://example.com/docs",
            "host: 192.0.2.10",
            "the sandbox runs at http://127.0.0.1:8000",
            "site: sandbox.localhost",
        ):
            self.assertEqual(findings(line), 0, line)

    def test_real_identifying_facts_are_loud(self) -> None:
        for line in (
            f"visit https://{CLIENT}",
            f"call {CLIENT_PHONE} to book",
            # Not "1234 Example Street" — values containing a documentation-
            # reserved word are exempt by design, which is correct and is
            # covered above.
            CLIENT_STREET,
        ):
            self.assertGreater(findings(line), 0, line)


class PrePushTest(unittest.TestCase):
    """BS-004 — the audit is only useful if something runs it."""

    def _run(
        self, findings_code: int, suite_code: int = 0, scan_code: int = 0,
        **env: str
    ) -> tuple[int, str]:
        import io
        import os
        from contextlib import redirect_stderr, redirect_stdout

        from buildsmith.tools import audit, prepush, secretscan

        # The recursion marker is inherited whenever this suite was itself
        # spawned by prepush; each test states its own value (via **env) so
        # results don't depend on how the suite was launched.
        env.setdefault(prepush._IN_SUITE, "")
        old_main, old_env = audit.main, {k: os.environ.get(k) for k in env}
        old_suite = prepush.run_suite
        old_scan = secretscan.main
        audit.main = lambda argv=None: findings_code  # type: ignore[assignment]
        prepush.run_suite = lambda: suite_code  # type: ignore[assignment]
        secretscan.main = lambda argv=None: scan_code  # type: ignore[assignment]
        os.environ.update(env)
        out = io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(out):
                code = prepush.main([])
        finally:
            audit.main = old_main  # type: ignore[assignment]
            prepush.run_suite = old_suite  # type: ignore[assignment]
            secretscan.main = old_scan  # type: ignore[assignment]
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return code, out.getvalue()

    def test_clean_audit_allows_the_push(self) -> None:
        code, _ = self._run(0)
        self.assertEqual(code, 0)

    def test_findings_refuse_the_push(self) -> None:
        code, out = self._run(1)
        self.assertEqual(code, 1)
        self.assertIn("PUSH REFUSED", out)

    def test_a_secret_finding_refuses_the_push(self) -> None:
        code, out = self._run(0, scan_code=1)
        self.assertEqual(code, 1)
        self.assertIn("PUSH REFUSED", out)

    def test_an_unrunnable_secret_scan_refuses_the_push(self) -> None:
        """Exit 2 from the scan means it never ran; passing the push anyway
        would make a missing gitleaks binary indistinguishable from a clean
        history (ADR-010). The 2 survives — git refuses on any nonzero, and
        a caller scripting on 1-vs-2 must still see "could not check"."""
        code, out = self._run(0, scan_code=2)
        self.assertEqual(code, 2)
        self.assertIn("PUSH REFUSED", out)

    def test_a_red_suite_refuses_the_push(self) -> None:
        code, out = self._run(0, suite_code=1)
        self.assertEqual(code, 1)
        self.assertIn("suite is red", out)

    def test_a_red_suite_names_the_escape_hatch(self) -> None:
        _, out = self._run(0, suite_code=1)
        self.assertIn("BUILDSMITH_SKIP_AUDIT=1", out)

    def test_the_spawned_suite_cannot_see_the_pushers_repository(self) -> None:
        """git hands the pre-push hook its repository location (GIT_DIR and
        family), and GIT_DIR overrides the `git -C <tempdir>` every fixture
        relies on. From a linked worktree that path is absolute, and the
        suite once committed its poison fixtures onto the very branch being
        pushed (#23). No GIT_* may reach the child suite."""
        import os
        import subprocess

        from buildsmith.tools import prepush

        captured: dict = {}
        old_run = subprocess.run
        old_env = {k: os.environ.get(k) for k in ("GIT_DIR", "GIT_INDEX_FILE")}
        os.environ["GIT_DIR"] = "/nonexistent/pusher/.git/worktrees/x"
        os.environ["GIT_INDEX_FILE"] = "/nonexistent/pusher/index"

        def fake_run(cmd, **kwargs):
            captured["env"] = kwargs.get("env")
            return type("P", (), {"returncode": 0})()

        subprocess.run = fake_run  # type: ignore[assignment]
        try:
            prepush.run_suite()
        finally:
            subprocess.run = old_run  # type: ignore[assignment]
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        child_env = captured["env"]
        self.assertIsNotNone(child_env, "the suite must run with an explicit env")
        leaked = [k for k in child_env if k.startswith("GIT_")]
        self.assertEqual(leaked, [], f"GIT_* leaked into the child suite: {leaked}")
        self.assertEqual(child_env.get(prepush._IN_SUITE), "1")

    def test_the_spawned_suite_is_buffered(self) -> None:
        """#1: several tests exercise a real refusal path on purpose and
        print the real message doing it — correct for what they test, but
        with no buffering, that output streamed straight through this
        subprocess's inherited stdout/stderr into the hook's own terminal,
        interleaved with the actual gate verdicts for the push in
        progress. -b discards a passing test's output and keeps it only
        for a failure."""
        from buildsmith.tools import prepush

        with mock.patch.object(
            prepush.subprocess, "run",
            return_value=mock.Mock(returncode=0),
        ) as run:
            prepush.run_suite()

        self.assertIn("-b", run.call_args.args[0])

    def test_the_suite_is_not_spawned_from_inside_itself(self) -> None:
        """prepush spawns the suite; the suite contains this test; without a
        recursion guard an unstubbed call would fork forever. The marker must
        short-circuit even when run_suite would report red."""
        from buildsmith.tools import prepush

        code, out = self._run(0, suite_code=1, **{prepush._IN_SUITE: "1"})
        self.assertEqual(code, 0)
        self.assertIn("not spawning it again", out)

    def test_the_escape_hatch_is_named_in_the_refusal(self) -> None:
        """An escape hatch nobody can find gets replaced by `--no-verify`,
        which skips every hook rather than this one."""
        _, out = self._run(1)
        self.assertIn("BUILDSMITH_SKIP_AUDIT=1", out)

    def test_the_escape_hatch_works_and_says_so(self) -> None:
        code, out = self._run(1, BUILDSMITH_SKIP_AUDIT="1")
        self.assertEqual(code, 0)
        self.assertIn("did not run", out)

    def test_the_hook_calls_the_module(self) -> None:
        """The shell hook must stay a two-line shim, not grow logic."""
        from pathlib import Path

        hook = Path(__file__).resolve().parents[1] / ".githooks" / "pre-push"
        body = [
            ln for ln in hook.read_text().splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]
        self.assertIn("buildsmith.tools.prepush", "\n".join(body))
        self.assertLessEqual(len(body), 5, "the hook grew logic; it should delegate")


if __name__ == "__main__":
    unittest.main()


class HistoryBlobTest(unittest.TestCase):
    """A scrubbed working tree is not a scrubbed repository.

    The audit reported "history: no findings" while 20 commits carried a client
    token in file content, because the history scope scanned commit messages and
    filenames only. That is the shape of leak that survives every cleanup: the
    tree is spotless, the paths are neutral, and the token sits in the object
    store waiting to be published (BS-025).
    """

    def test_a_token_only_in_history_is_found(self) -> None:
        import tempfile
        from pathlib import Path

        from buildsmith.tools import audit

        token = "acme" + "corp"
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            run = lambda *a: run_git("-C", str(repo), *a)  # noqa: E731
            run("init", "-q", "-b", "main")
            run("config", "user.name", "t")
            run("config", "user.email", "t@example.invalid")

            leak = repo / "notes.md"
            leak.write_text(f"the {token} rollout\n")
            run("add", "-A")
            run("commit", "-q", "-m", "TKT-1: notes")

            # Scrub the working tree, exactly as a well-meaning fix would.
            leak.write_text("the rollout\n")
            run("add", "-A")
            run("commit", "-q", "-m", "TKT-2: scrub")

            self.assertNotIn(token, leak.read_text(), "working tree should be clean")

            old_root = audit.ROOT
            audit.ROOT = repo
            try:
                blobs = [
                    line.split()
                    for line in audit._git(
                        "cat-file", "--batch-all-objects",
                        "--batch-check=%(objectname) %(objecttype) %(objectsize)",
                    ).splitlines()
                ]
                contents = [
                    audit._git("cat-file", "blob", p[0])
                    for p in blobs if len(p) == 3 and p[1] == "blob"
                ]
            finally:
                audit.ROOT = old_root

            self.assertTrue(
                any(token in c for c in contents),
                "the token must still be reachable in the object store — if this "
                "fails the fixture is wrong, not the scanner",
            )
