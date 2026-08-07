"""The two transports must read the dev instance identically.

`capture` can read the dev instance by executing inside its container (`bench`)
or over HTTP (`rest`). The container build needs `rest`, because mounting a
Docker socket into an image designers run is not acceptable.

The risk is not that `rest` fails — a failure is loud. It is that `rest` returns
a *subtly different shape*: an integer where bench produced a string, `None`
where bench produced `""`, a JSON string where bench produced a parsed list.
Every one of those feeds `_content_hash`, which is what `drift` and
`publish-verify` compare. A transport that quietly changed the hash would make
every capture look like drift, and the tools would be reporting on their own
plumbing rather than on the site.

So this asserts equality of the whole structure, not just of the counts. It
needs a running sandbox and a token, and skips loudly rather than passing when
either is missing — a transport-equivalence test that silently did not run is
the same as not having one.
"""

from __future__ import annotations

import json
import os
import unittest

from buildsmith.tools import capture_dev


def _sandbox_available() -> str | None:
    """Why we cannot run, or None if we can."""
    if not os.environ.get("BUILDSMITH_FRAPPE_TOKEN"):
        return (
            "BUILDSMITH_FRAPPE_TOKEN is not set. Mint one with "
            "`buildsmith sandbox token` — without it the REST transport cannot "
            "authenticate and this equivalence is unverified."
        )
    try:
        from buildsmith.tools.frappe_client import from_env

        if not from_env().ping():
            return "the dev instance did not answer /api/method/ping"
    except Exception as exc:  # noqa: BLE001 — any failure means "cannot check"
        return f"the dev instance is unreachable: {exc}"
    return None


class TransportEquivalenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        reason = _sandbox_available()
        if reason:
            raise unittest.SkipTest(reason)
        cls.bench = capture_dev.read_state("sandbox.localhost", transport="bench")
        cls.rest = capture_dev.read_state("sandbox.localhost", transport="rest")

    def test_content_hash_matches(self) -> None:
        """The single value `drift` and `publish-verify` actually compare."""
        self.assertEqual(
            capture_dev._content_hash(self.bench),
            capture_dev._content_hash(self.rest),
        )

    def test_whole_state_matches(self) -> None:
        """Not just the hashed subset — the hash excludes names and settings,
        and a transport that dropped those would pass the check above."""
        self.assertEqual(
            json.dumps(self.bench, sort_keys=True),
            json.dumps(self.rest, sort_keys=True),
        )

    def test_field_types_match(self) -> None:
        """The specific failure this exists for.

        `published` as `1` and as `"1"` both look right in a JSON dump read by
        eye, and both hash differently.
        """
        for page_b, page_r in zip(
            sorted(self.bench["pages"], key=lambda p: p["name"]),
            sorted(self.rest["pages"], key=lambda p: p["name"]),
            strict=True,  # unequal counts IS the transport divergence
        ):
            for field in capture_dev.PAGE_FIELDS:
                self.assertEqual(
                    type(page_b[field]),
                    type(page_r[field]),
                    f"{page_b['route']!r}.{field}: bench "
                    f"{type(page_b[field]).__name__} vs rest "
                    f"{type(page_r[field]).__name__}",
                )

    def test_blocks_are_parsed_not_strings(self) -> None:
        """Blocks arrive as a JSON string over REST and must be parsed.

        Left as a string this is the most damaging case: everything downstream
        keeps working, and the clone silently contains no blocks.
        """
        for page in self.rest["pages"]:
            self.assertIsInstance(page["blocks"], list, page["route"])


class TransportSelectionTest(unittest.TestCase):
    """Selection must be predictable and must never widen the target."""

    def test_a_non_local_target_is_refused_on_every_transport(self) -> None:
        for transport in ("auto", "rest", "bench"):
            with self.assertRaises(SystemExit, msg=transport):
                capture_dev.read_state("acme" + ".com", transport=transport)


if __name__ == "__main__":
    unittest.main()
