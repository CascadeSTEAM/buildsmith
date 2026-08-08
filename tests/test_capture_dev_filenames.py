"""#27: capture_dev must not let colliding route slugs clobber each other.

`capture()` used to derive each page's on-disk filename from its route:
`(route or "home").replace("/", "_")`. Two distinct routes can slugify to the
same string — e.g. `a/b` and `a_b` both become `a_b.json` — so the second
page written silently overwrote the first's file. `simulate.load_state()`
reads that directory back for the TRAP-001 safety check, so the loss was
silent there too: `pages_using()` would understate which pages a component
touches.

No live sandbox needed: this stubs the REST client capture_dev reads through,
same pattern as test_capture_dev_draft_blocks.py.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from buildsmith.tools import capture_dev


class StubClient:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages

    def get_list(self, doctype, fields):
        if doctype == "Builder Page":
            return self._pages
        return []

    def get(self, doctype, name):
        return {"home_page": None}


def _page(name: str, route: str) -> dict:
    row = {field: None for field in capture_dev.PAGE_FIELDS}
    row.update({"name": name, "route": route, "blocks": "[]", "draft_blocks": "[]"})
    return row


@contextmanager
def _captured(pages: list[dict]):
    """Run `capture()` against a stubbed REST client, yielding (manifest, out).

    Shared by every test below so a future change to how `capture()` is
    invoked (env var, mock target, client kwarg) is a one-line edit instead
    of a three-way copy-paste to keep in sync.
    """
    client = StubClient(pages)
    with mock.patch("buildsmith.tools.frappe_client.from_env", return_value=client):
        with mock.patch.dict("os.environ", {"BUILDSMITH_FRAPPE_TOKEN": "x"}):
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "dev-state"
                manifest = capture_dev.capture(
                    "testsite", target="sandbox.localhost", transport="rest", out=out,
                )
                yield manifest, out


class CollidingRouteSlugsTest(unittest.TestCase):
    def test_both_pages_survive_capture(self):
        pages = [_page("page-aaa11111", "a/b"), _page("page-bbb22222", "a_b")]
        with _captured(pages) as (manifest, out):
            written = sorted((out / "pages").glob("*.json"))
            self.assertEqual(len(written), 2, written)
            routes = {json.loads(p.read_text())["route"] for p in written}
            self.assertEqual(routes, {"a/b", "a_b"})
            self.assertEqual(manifest["counts"]["pages"], 2)

    def test_home_route_and_literal_home_route_both_survive(self):
        pages = [_page("page-ccc33333", ""), _page("page-ddd44444", "home")]
        with _captured(pages) as (_manifest, out):
            written = sorted((out / "pages").glob("*.json"))
            self.assertEqual(len(written), 2, written)

    def test_filenames_are_keyed_by_name_not_route(self):
        pages = [_page("page-eee55555", "some/route")]
        with _captured(pages) as (_manifest, out):
            self.assertTrue((out / "pages" / "page-eee55555.json").exists())


class UnsafeFilenameKeyTest(unittest.TestCase):
    """#27 review: a page name is normally a safe `page-<hash8>` (TRAP-012),
    but ADR-008 records it can become choosable — capture() must refuse a
    name that would escape `dev-state/pages/` rather than crash or clobber."""

    def test_a_slash_in_the_page_name_is_refused_not_written(self):
        pages = [_page("../escape", "/x")]
        with self.assertRaises(SystemExit):
            with _captured(pages):
                pass

    def test_a_normal_name_passes_through_unmodified(self):
        self.assertEqual(
            capture_dev._filename_key("page-aaa11111", field="page name"),
            "page-aaa11111",
        )


if __name__ == "__main__":
    unittest.main()
