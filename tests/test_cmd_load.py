"""#13: `buildsmith load` — the deferred counterpart to `clone --no-load`.

`cmd_clone` already writes everything `load_dev.load` needs to
`sites/<site>/build/` unconditionally, before the `--no-load` gate. These
pin the CLI wiring: the new subcommand reaches `load_dev.load` with the
right arguments, and its existing "no build/ yet" refusal still reads as
exit 2 through the new entry point, not just the old one inside `clone`.
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from buildsmith import cli  # noqa: E402
from buildsmith.tools import load_dev  # noqa: E402


class LoadSubcommandWiringTest(unittest.TestCase):
    def test_site_and_defaults_reach_load_dev(self):
        with mock.patch.object(load_dev, "load") as fake_load:
            code = cli.main(["load", "--site", "example"])
        self.assertEqual(code, 0)
        fake_load.assert_called_once_with(
            "example", with_assets=True, target="sandbox.localhost")

    def test_no_assets_and_target_pass_through(self):
        with mock.patch.object(load_dev, "load") as fake_load:
            code = cli.main(["load", "--site", "example",
                             "--no-assets", "--target", "roundtrip.localhost"])
        self.assertEqual(code, 0)
        fake_load.assert_called_once_with(
            "example", with_assets=False, target="roundtrip.localhost")

    def test_missing_build_directory_is_exit_2_not_a_traceback(self):
        # The exact scenario --no-load enables: crawl+convert ran, but
        # nothing was ever loaded. load_dev.load's own CouldNotCheck must
        # still map to exit 2 through this new entry point.
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "sites" / "example").mkdir(parents=True)
            err = io.StringIO()
            with mock.patch.object(load_dev, "ROOT", Path(tmp)), \
                 mock.patch.object(load_dev, "_sandbox_running"), \
                 redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = cli.main(["load", "--site", "example"])
        self.assertEqual(code, 2)
        self.assertIn("COULD NOT CHECK", err.getvalue())
        self.assertIn("buildsmith build", err.getvalue())


if __name__ == "__main__":
    unittest.main()
