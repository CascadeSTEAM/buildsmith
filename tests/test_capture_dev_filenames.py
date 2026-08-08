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
import unittest
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


class CollidingRouteSlugsTest(unittest.TestCase):
    def test_both_pages_survive_capture(self):
        pages = [_page("page-aaa11111", "a/b"), _page("page-bbb22222", "a_b")]
        client = StubClient(pages)
        with mock.patch("buildsmith.tools.frappe_client.from_env", return_value=client):
            with mock.patch.dict("os.environ", {"BUILDSMITH_FRAPPE_TOKEN": "x"}):
                import tempfile
                from pathlib import Path

                with tempfile.TemporaryDirectory() as tmp:
                    out = Path(tmp) / "dev-state"
                    manifest = capture_dev.capture(
                        "testsite", target="sandbox.localhost",
                        transport="rest", out=out,
                    )
                    written = sorted((out / "pages").glob("*.json"))
                    self.assertEqual(len(written), 2, written)
                    routes = {
                        json.loads(p.read_text())["route"] for p in written
                    }
                    self.assertEqual(routes, {"a/b", "a_b"})
                    self.assertEqual(manifest["counts"]["pages"], 2)

    def test_home_route_and_literal_home_route_both_survive(self):
        pages = [_page("page-ccc33333", ""), _page("page-ddd44444", "home")]
        client = StubClient(pages)
        with mock.patch("buildsmith.tools.frappe_client.from_env", return_value=client):
            with mock.patch.dict("os.environ", {"BUILDSMITH_FRAPPE_TOKEN": "x"}):
                import tempfile
                from pathlib import Path

                with tempfile.TemporaryDirectory() as tmp:
                    out = Path(tmp) / "dev-state"
                    capture_dev.capture(
                        "testsite", target="sandbox.localhost",
                        transport="rest", out=out,
                    )
                    written = sorted((out / "pages").glob("*.json"))
                    self.assertEqual(len(written), 2, written)

    def test_filenames_are_keyed_by_name_not_route(self):
        pages = [_page("page-eee55555", "some/route")]
        client = StubClient(pages)
        with mock.patch("buildsmith.tools.frappe_client.from_env", return_value=client):
            with mock.patch.dict("os.environ", {"BUILDSMITH_FRAPPE_TOKEN": "x"}):
                import tempfile
                from pathlib import Path

                with tempfile.TemporaryDirectory() as tmp:
                    out = Path(tmp) / "dev-state"
                    capture_dev.capture(
                        "testsite", target="sandbox.localhost",
                        transport="rest", out=out,
                    )
                    self.assertTrue((out / "pages" / "page-eee55555.json").exists())


if __name__ == "__main__":
    unittest.main()
