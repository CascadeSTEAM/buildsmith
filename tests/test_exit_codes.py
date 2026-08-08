"""Exit codes are a contract: 0 proved, 1 found a problem, 2 could not check.

These tests pin the compositional layer — cli.main() and each tool's main() —
because that is where the contract kept leaking: a library refusal mapped to
exit 1 when it meant "could not check", a view filter turned findings into
exit 0, and a vacuous simulation printed a collapse warning it never found.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from buildsmith import cli  # noqa: E402
from buildsmith.errors import CouldNotCheck  # noqa: E402
from buildsmith.tools import audit, simulate  # noqa: E402


class CouldNotCheckMapsToExit2(unittest.TestCase):
    def test_a_missing_precondition_is_exit_2_not_1(self):
        # drift with no crawl directory: "has live changed?" has no answer.
        # Before CouldNotCheck existed this was SystemExit(str) -> exit 1,
        # which reads as "found a problem" to anything scripting the CLI.
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = cli.main(["drift", "--site", "no-such-site",
                             "--source", "http://127.0.0.1:1"])
        self.assertEqual(code, 2)
        self.assertIn("COULD NOT CHECK", err.getvalue())

    def test_could_not_check_is_a_system_exit(self):
        # An escape that misses the CLI handler must still refuse, not pass.
        self.assertTrue(issubclass(CouldNotCheck, SystemExit))

    def test_cannot_prove_is_could_not_check(self):
        # One handler catches both; two names, one meaning.
        from buildsmith.workflows.optimize.tokenize import CannotProve

        self.assertTrue(issubclass(CannotProve, CouldNotCheck))


class SimulateVacuousIsExit2(unittest.TestCase):
    def test_an_empty_export_exits_2_with_an_honest_message(self):
        # It used to print "Refusing: this payload would collapse nodes ..."
        # (a finding it never made) and exit 1.
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text(json.dumps({"pages": [], "components": {}}))
            payload = Path(tmp) / "payload.json"
            payload.write_text(json.dumps({
                "component_id": "site-header",
                "block": {"element": "header", "blockId": "aaaabbbbccccdddd"},
            }))
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = simulate.main(["--state", str(state),
                                      "--payload", str(payload)])
        self.assertEqual(code, 2)
        self.assertIn("NOTHING CHECKED", err.getvalue())
        self.assertNotIn("collapse nodes", err.getvalue())


class SimulateMalformedInputIsExit2(unittest.TestCase):
    """#3: a malformed payload or an incomplete export is "could not check",
    never a raw traceback — the same contract as the vacuous-export case."""

    def _state(self, tmp: Path) -> Path:
        state = Path(tmp) / "state.json"
        state.write_text(json.dumps({"pages": [], "components": {}}))
        return state

    def test_a_payload_with_no_component_id_is_exit_2_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self._state(tmp)
            payload = Path(tmp) / "payload.json"
            payload.write_text(json.dumps({"block": {"element": "div"}}))
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["simulate", "--state", str(state),
                                 "--payload", str(payload)])
        self.assertEqual(code, 2)
        self.assertIn("COULD NOT CHECK", err.getvalue())

    def test_an_incomplete_export_is_exit_2_not_a_traceback(self):
        # A page already uses the component, but the export omits it —
        # comparing against nothing would pass vacuously (simulate.py).
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text(json.dumps({
                "components": {},
                "pages": [{"name": "home", "route": "/", "blocks": [
                    {"blockId": "shell", "referenceBlockId": "root",
                     "extendedFromComponent": "site-header", "element": None,
                     "children": []},
                ]}],
            }))
            payload = Path(tmp) / "payload.json"
            payload.write_text(json.dumps({
                "component_id": "site-header",
                "block": {"element": "header", "blockId": "root"},
            }))
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["simulate", "--state", str(state),
                                 "--payload", str(payload)])
        self.assertEqual(code, 2)
        self.assertIn("COULD NOT CHECK", err.getvalue())
        self.assertIn("incomplete", err.getvalue())


class AuditKindFilterNeverChangesTheVerdict(unittest.TestCase):
    """--kind narrows what is shown; the exit code follows the full report."""

    def _report_with_token_finding(self) -> audit.Report:
        return audit.Report(
            findings=[audit.Finding(where="somefile", kind="token",
                                    detail="client token")],
            tokens_available=True,
            scanned={"files": 1},
        )

    def test_findings_of_a_hidden_kind_still_fail_the_run(self):
        # `--kind fact` on a repo with a token finding used to print
        # "No findings." and exit 0 — a leak shipped on the word of a filter.
        with mock.patch.object(audit, "audit",
                               return_value=self._report_with_token_finding()):
            out = io.StringIO()
            with redirect_stdout(out):
                code = audit.main(["--kind", "fact"])
        self.assertEqual(code, 1)
        self.assertIn("other kinds exist", out.getvalue())

    def test_json_and_text_paths_agree(self):
        with mock.patch.object(audit, "audit",
                               return_value=self._report_with_token_finding()):
            out = io.StringIO()
            with redirect_stdout(out):
                json_code = audit.main(["--kind", "fact", "--json"])
        self.assertEqual(json_code, 1)
        self.assertEqual(json.loads(out.getvalue())["hidden_by_kind"], 1)

    def test_a_clean_report_still_passes_with_kind(self):
        clean = audit.Report(findings=[], tokens_available=True,
                             scanned={"files": 1})
        with mock.patch.object(audit, "audit", return_value=clean):
            with redirect_stdout(io.StringIO()):
                code = audit.main(["--kind", "fact"])
        self.assertEqual(code, 0)


class VerifyProblemOutranksUnchecked(unittest.TestCase):
    """A found problem keeps exit 1 even when a later leg could not run.

    The regression this pins: visual_check's missing-features.json refusal
    became CouldNotCheck, which escaped cmd_verify past the accumulated
    problems straight to main()'s handler — so a verify that HAD found a
    conformance problem exited 2 ("could not check") instead of 1.
    """

    def _run_verify(self, *, conformance_ok: bool) -> int:
        import os
        import types

        from buildsmith.tools import conformance, visual_check

        shape = types.SimpleNamespace(ok=conformance_ok, pages_checked=3)

        def missing_inventory(*a, **kw):
            raise CouldNotCheck("features.json does not exist")

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "sites" / "x" / "build").mkdir(parents=True)
            with mock.patch.dict(os.environ, {"BUILDSMITH_ROOT": tmp}), \
                 mock.patch.object(conformance, "check_payload_dir",
                                   return_value=shape), \
                 mock.patch.object(conformance, "report"), \
                 mock.patch.object(visual_check, "check_site",
                                   side_effect=missing_inventory):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    return cli.main(["verify", "--site", "x",
                                     "--clone", "http://127.0.0.1:1"])

    def test_found_problem_plus_unrunnable_browser_check_is_exit_1(self):
        self.assertEqual(self._run_verify(conformance_ok=False), 1)

    def test_clean_legs_plus_unrunnable_browser_check_is_exit_2(self):
        self.assertEqual(self._run_verify(conformance_ok=True), 2)


if __name__ == "__main__":
    unittest.main()
