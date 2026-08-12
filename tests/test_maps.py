"""Tests for the location-map embed primitive.

Covers the OSM/Google URL builders in isolation, `location_map`'s input
validation (especially the OSM-needs-coordinates refusal, which is the whole
reason this module does not just take an address for every provider), and one
integration test proving the emitted tree survives the same `@token`
resolution and `compose()` pipeline every other component goes through.
"""

from __future__ import annotations

import sys
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from buildsmith.primitives.components import compose  # noqa: E402
from buildsmith.primitives.maps import (  # noqa: E402
    EmbedError,
    google_embed_src,
    location_map,
    osm_embed_src,
)
from buildsmith.primitives.tokens import Applied  # noqa: E402
from buildsmith.workflows.theme.build import resolve_tokens  # noqa: E402


class OsmEmbedSrc(unittest.TestCase):
    def test_bbox_and_marker_bracket_the_point(self):
        url = osm_embed_src(45.0, -73.0, span=0.01)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertTrue(url.startswith("https://www.openstreetmap.org/export/embed.html?"))
        self.assertEqual(query["marker"], ["45.0,-73.0"])
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in query["bbox"][0].split(","))
        self.assertAlmostEqual(min_lon, -73.01)
        self.assertAlmostEqual(max_lon, -72.99)
        self.assertAlmostEqual(min_lat, 44.99)
        self.assertAlmostEqual(max_lat, 45.01)
        self.assertEqual(query["layer"], ["mapnik"])

    def test_default_span_is_positive_and_small(self):
        # Not pinned to a specific number — just "narrow enough to be useful,
        # wide enough to have shipped a default at all".
        url = osm_embed_src(0.0, 0.0)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in query["bbox"][0].split(","))
        self.assertGreater(max_lon - min_lon, 0)
        self.assertLess(max_lon - min_lon, 1)
        self.assertGreater(max_lat - min_lat, 0)

    def test_lat_out_of_range_refused(self):
        with self.assertRaises(EmbedError):
            osm_embed_src(91.0, 0.0)
        with self.assertRaises(EmbedError):
            osm_embed_src(-91.0, 0.0)

    def test_lon_out_of_range_refused(self):
        with self.assertRaises(EmbedError):
            osm_embed_src(0.0, 181.0)
        with self.assertRaises(EmbedError):
            osm_embed_src(0.0, -181.0)

    def test_non_positive_span_refused(self):
        with self.assertRaises(EmbedError):
            osm_embed_src(0.0, 0.0, span=0)
        with self.assertRaises(EmbedError):
            osm_embed_src(0.0, 0.0, span=-0.01)


class GoogleEmbedSrc(unittest.TestCase):
    def test_address_is_the_query_and_output_is_embed(self):
        url = google_embed_src("221B Baker Street, London")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertTrue(url.startswith("https://maps.google.com/maps?"))
        self.assertEqual(query["q"], ["221B Baker Street, London"])
        self.assertEqual(query["output"], ["embed"])

    def test_empty_address_refused(self):
        with self.assertRaises(EmbedError):
            google_embed_src("")
        with self.assertRaises(EmbedError):
            google_embed_src("   ")


class LocationMap(unittest.TestCase):
    def test_osm_without_coordinates_is_refused_not_geocoded(self):
        with self.assertRaises(EmbedError) as caught:
            location_map(address="1 Example Way, Nowhere")
        self.assertIn("does not geocode", str(caught.exception))

    def test_osm_with_coordinates_embeds_the_osm_url(self):
        root = location_map(address="1 Example Way, Nowhere", lat=0.0, lon=0.0)
        iframe = root["children"][0]
        self.assertEqual(root["element"], "div")
        self.assertEqual(iframe["element"], "iframe")
        self.assertTrue(iframe["attributes"]["src"].startswith(
            "https://www.openstreetmap.org/export/embed.html?"
        ))
        self.assertEqual(iframe["attributes"]["title"], "Map: 1 Example Way, Nowhere")

    def test_google_needs_no_coordinates(self):
        root = location_map(
            address="1 Example Way, Nowhere", provider="google",
        )
        iframe = root["children"][0]
        self.assertTrue(iframe["attributes"]["src"].startswith("https://maps.google.com/maps?"))

    def test_unknown_provider_refused(self):
        with self.assertRaises(EmbedError):
            location_map(address="somewhere", provider="bing", lat=0.0, lon=0.0)

    def test_empty_address_refused(self):
        with self.assertRaises(EmbedError):
            location_map(address="", lat=0.0, lon=0.0)

    def test_sizing_and_border_default_to_token_sigils(self):
        root = location_map(address="1 Example Way, Nowhere", lat=0.0, lon=0.0)
        styles = root["baseStyles"]
        self.assertEqual(styles["width"], "@map-width")
        self.assertEqual(styles["height"], "@map-height")
        self.assertEqual(styles["borderRadius"], "@map-radius")
        self.assertEqual(styles["borderColor"], "@map-border")
        # Longhand, not the `border` shorthand — see the module docstring on
        # why a shorthand is where a literal colour would hide.
        self.assertNotIn("border", styles)

    def test_a_literal_can_replace_any_sigil(self):
        root = location_map(
            address="1 Example Way, Nowhere", lat=0.0, lon=0.0,
            width="400px", height="300px", border_radius="0", border_color="#000",
        )
        styles = root["baseStyles"]
        self.assertEqual(styles["width"], "400px")
        self.assertEqual(styles["borderColor"], "#000")

    def test_resolves_and_composes_like_any_other_component_spec(self):
        # The pipeline every design/components/*.json spec goes through in
        # build_site(): resolve @token sigils against the applied map, then
        # compose() into a real Component payload.
        applied = Applied.from_dict(
            {
                "tokens": {
                    "map-width": {"uuid": "u-w", "value": "480px"},
                    "map-height": {"uuid": "u-h", "value": "320px"},
                    "map-radius": {"uuid": "u-r", "value": "8px"},
                    "map-border": {"uuid": "u-b", "value": "#cccccc"},
                }
            }
        )
        root = location_map(address="1 Example Way, Nowhere", lat=0.0, lon=0.0)
        resolved = resolve_tokens(root, applied)
        component = compose(
            component_id="location-map",
            component_name="Location Map",
            root=resolved,
            applied=applied,
        )
        self.assertEqual(component.component_id, "location-map")
        border_colour = component.block["baseStyles"]["borderColor"]
        self.assertEqual(border_colour, "var(--u-b, #cccccc)")


if __name__ == "__main__":
    unittest.main()
