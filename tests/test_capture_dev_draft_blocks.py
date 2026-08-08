"""#4: capture_dev must capture draft_blocks, not just blocks.

simulate.pages_using() checks draft_blocks explicitly — an unpublished draft
is still damage waiting to be published — but capture_dev's PAGE_FIELDS
never asked for it, so a captured export could never surface that class of
finding. No live sandbox needed: this stubs the REST client capture_dev
reads through.
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


class DraftBlocksCaptureTest(unittest.TestCase):
    def test_page_fields_asks_for_draft_blocks(self):
        self.assertIn("draft_blocks", capture_dev.PAGE_FIELDS)

    def test_rest_transport_parses_draft_blocks_into_a_list(self):
        draft = [{"blockId": "unpublished-shell", "extendedFromComponent": "site-header"}]
        row = {field: None for field in capture_dev.PAGE_FIELDS}
        row.update({
            "name": "page-1", "route": "/draft-page", "blocks": "[]",
            "draft_blocks": json.dumps(draft),
        })
        client = StubClient([row])
        with mock.patch("buildsmith.tools.frappe_client.from_env", return_value=client):
            out = capture_dev._read_state_rest("sandbox.localhost")
        self.assertEqual(len(out["pages"]), 1)
        self.assertEqual(out["pages"][0]["draft_blocks"], draft)

    def test_a_missing_draft_blocks_column_parses_as_an_empty_list(self):
        row = {field: None for field in capture_dev.PAGE_FIELDS}
        row.update({"name": "page-1", "route": "/x", "blocks": "[]"})
        client = StubClient([row])
        with mock.patch("buildsmith.tools.frappe_client.from_env", return_value=client):
            out = capture_dev._read_state_rest("sandbox.localhost")
        self.assertEqual(out["pages"][0]["draft_blocks"], [])


def _minimal_state(**page_overrides) -> dict:
    page = {
        "route": "/x", "page_title": "X", "blocks": [], "head_html": "",
        "draft_blocks": [],
    }
    page.update(page_overrides)
    return {"pages": [page], "components": [], "tokens": {}}


class ContentHashSeesDraftBlocksTest(unittest.TestCase):
    """A drift check that can't see a draft edit isn't checking what matters."""

    def test_a_draft_only_change_changes_the_hash(self):
        clean = _minimal_state()
        edited = _minimal_state(draft_blocks=[{"blockId": "new-shell"}])
        self.assertNotEqual(
            capture_dev._content_hash(clean), capture_dev._content_hash(edited))

    def test_an_unchanged_draft_does_not_change_the_hash(self):
        a = _minimal_state(draft_blocks=[{"blockId": "same"}])
        b = _minimal_state(draft_blocks=[{"blockId": "same"}])
        self.assertEqual(capture_dev._content_hash(a), capture_dev._content_hash(b))

    def test_a_missing_draft_blocks_key_does_not_crash(self):
        state = _minimal_state()
        del state["pages"][0]["draft_blocks"]
        capture_dev._content_hash(state)  # must not raise


class PublishVerifyDraftBlocksTest(unittest.TestCase):
    """publish_verify's rehearsal must serialize draft_blocks like blocks —
    Frappe stores the column as JSON text, not a Python list (#26 review)."""

    def test_apply_script_serializes_draft_blocks_not_forwards_it_raw(self):
        from buildsmith.tools import publish_verify

        self.assertIn('payload["draft_blocks"] = json.dumps', publish_verify.APPLY)
        # excluded from the blanket copy-through so the explicit json.dumps
        # line above is the only place it's set — a raw list slipping
        # through the blanket copy would insert the wrong type (#26 review).
        self.assertIn('"name", "blocks", "draft_blocks"', publish_verify.APPLY)


if __name__ == "__main__":
    unittest.main()
