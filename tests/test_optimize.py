"""Tests for the optimize workflow's oracle machinery: the PNG codec, the
pixel diff, the script-dependency scanner, the rendering oracle's report
logic, and the deterministic shot-naming scheme.

Everything here is pure-python and needs nothing installed and nothing
running. `run_oracle` is exercised with an injected `shooter` writing into a
tempdir, so no playwright and no network — the same rule test_traps.py states
for its own domain: a check that cannot run must never look like it passed.

    python3 -m unittest tests.test_optimize -v
"""

from __future__ import annotations

import json
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from buildsmith.tools import capture_dev  # noqa: E402
from buildsmith.workflows.optimize import (
    baseline,  # noqa: E402
    collapse,  # noqa: E402
    componentize,  # noqa: E402
    fonts,  # noqa: E402
    png,  # noqa: E402
    tokenize,  # noqa: E402
)
from buildsmith.workflows.optimize import shots as shots_mod  # noqa: E402
from buildsmith.workflows.optimize.oracle import (  # noqa: E402
    CannotCheck,
    run_oracle,
)

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


# -- manual PNG construction, mirroring png.encode() but exposing filter
#    type and bit depth/colour type/interlace as knobs the codec must refuse
#    or accept correctly -----------------------------------------------------

def _chunk(ctype: bytes, body: bytes) -> bytes:
    return (struct.pack(">I", len(body)) + ctype + body
            + struct.pack(">I", zlib.crc32(ctype + body) & 0xFFFFFFFF))


def _raw_png(width: int, height: int, bitdepth: int, colourtype: int,
            interlace: int, idat_body: bytes, extra_chunks: bytes = b"") -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, bitdepth, colourtype, 0, 0,
                       interlace)
    return (_PNG_SIG + _chunk(b"IHDR", ihdr) + extra_chunks
            + _chunk(b"IDAT", idat_body) + _chunk(b"IEND", b""))


def _forward_filter(rows: list[bytes], ftype: int, bpp: int) -> bytes:
    """The inverse of png._unfilter: apply `ftype` to reference scanlines so
    the decoder can be asked to recover exactly those reference bytes."""
    out = bytearray()
    prev = bytearray(len(rows[0]))
    for row in rows:
        line = bytearray(row)
        filt = bytearray(len(line))
        for i in range(len(line)):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if ftype == 1:              # Sub
                pred = a
            elif ftype == 2:            # Up
                pred = b
            elif ftype == 3:            # Average
                pred = (a + b) >> 1
            elif ftype == 4:            # Paeth
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            else:
                raise ValueError(ftype)
            filt[i] = (line[i] - pred) & 0xFF
        out.append(ftype)
        out.extend(filt)
        prev = line
    return bytes(out)


def _png_with_filter(width: int, height: int, channels: int,
                     rows: list[bytes], ftype: int) -> bytes:
    colourtype = {1: 0, 3: 2, 4: 6}[channels]
    raw = _forward_filter(rows, ftype, channels)
    return _raw_png(width, height, 8, colourtype, 0, zlib.compress(raw, 6))


def _varied_pixels(width: int, height: int, channels: int) -> bytearray:
    return bytearray((i * 53 + 7) % 256 for i in range(width * height * channels))


class PngRoundTrip(unittest.TestCase):
    def test_rgb_round_trips(self):
        img = png.Image(5, 4, 3, _varied_pixels(5, 4, 3))
        decoded = png.decode(png.encode(img))
        self.assertEqual(decoded.width, 5)
        self.assertEqual(decoded.height, 4)
        self.assertEqual(decoded.channels, 3)
        self.assertEqual(bytes(decoded.pixels), bytes(img.pixels))

    def test_rgba_round_trips(self):
        img = png.Image(5, 4, 4, _varied_pixels(5, 4, 4))
        decoded = png.decode(png.encode(img))
        self.assertEqual(decoded.width, 5)
        self.assertEqual(decoded.height, 4)
        self.assertEqual(decoded.channels, 4)
        self.assertEqual(bytes(decoded.pixels), bytes(img.pixels))


class DecodeFilters(unittest.TestCase):
    """Sub, Up, Average, Paeth — one hand-built PNG per filter type, each
    recoverable to the same two reference scanlines."""

    ROW0 = bytes([10, 20, 30, 40, 50, 60, 70, 80, 90])
    ROW1 = bytes([15, 25, 35, 45, 55, 65, 75, 85, 95])
    WIDTH, HEIGHT, CHANNELS = 3, 2, 3

    def _decode(self, ftype: int) -> png.Image:
        data = _png_with_filter(self.WIDTH, self.HEIGHT, self.CHANNELS,
                                [self.ROW0, self.ROW1], ftype)
        return png.decode(data)

    def test_sub(self):
        img = self._decode(1)
        self.assertEqual(bytes(img.pixels), self.ROW0 + self.ROW1)

    def test_up(self):
        img = self._decode(2)
        self.assertEqual(bytes(img.pixels), self.ROW0 + self.ROW1)

    def test_average(self):
        img = self._decode(3)
        self.assertEqual(bytes(img.pixels), self.ROW0 + self.ROW1)

    def test_paeth(self):
        img = self._decode(4)
        self.assertEqual(bytes(img.pixels), self.ROW0 + self.ROW1)


class DecodeRejections(unittest.TestCase):
    def test_bad_signature_is_corrupt(self):
        with self.assertRaises(png.CorruptPng):
            png.decode(b"not a png at all, just some bytes" * 4)

    def test_palette_chunk_is_unsupported(self):
        data = _raw_png(1, 1, 8, 2, 0, idat_body=b"",
                        extra_chunks=_chunk(b"PLTE", b"\x00\x00\x00"))
        with self.assertRaises(png.UnsupportedPng):
            png.decode(data)

    def test_16bit_depth_is_unsupported(self):
        data = _raw_png(1, 1, 16, 2, 0, idat_body=b"")
        with self.assertRaises(png.UnsupportedPng):
            png.decode(data)

    def test_interlaced_is_unsupported(self):
        data = _raw_png(1, 1, 8, 2, 1, idat_body=b"")
        with self.assertRaises(png.UnsupportedPng):
            png.decode(data)


class Diffing(unittest.TestCase):
    WIDTH, HEIGHT, CHANNELS = 4, 3, 3

    def _image(self, fill=0) -> png.Image:
        total = self.WIDTH * self.HEIGHT * self.CHANNELS
        return png.Image(self.WIDTH, self.HEIGHT, self.CHANNELS,
                         bytearray([fill] * total))

    def test_identical_images_differ_nowhere(self):
        a = self._image(50)
        b = self._image(50)
        result = png.diff(a, b)
        self.assertEqual(result.differing, 0)
        self.assertTrue(result.within(0.0))

    def test_single_changed_pixel_is_found_with_correct_bbox_and_ratio(self):
        a = self._image(50)
        b = self._image(50)
        x, y = 2, 1
        idx = (y * self.WIDTH + x) * self.CHANNELS
        b.pixels[idx] = 50 + 10  # well beyond default tolerance of 2
        result = png.diff(a, b)
        self.assertEqual(result.differing, 1)
        self.assertEqual(result.bbox, (x, y, x, y))
        self.assertEqual(result.ratio, 1 / (self.WIDTH * self.HEIGHT))

    def test_delta_exactly_at_tolerance_does_not_count(self):
        a = self._image(50)
        b = self._image(50)
        b.pixels[0] = 50 + 2  # == tolerance
        result = png.diff(a, b, tolerance=2)
        self.assertEqual(result.differing, 0)

    def test_delta_one_past_tolerance_counts(self):
        a = self._image(50)
        b = self._image(50)
        b.pixels[0] = 50 + 3  # tolerance + 1
        result = png.diff(a, b, tolerance=2)
        self.assertEqual(result.differing, 1)

    def test_size_mismatch_is_never_within_threshold(self):
        a = self._image(50)
        b = png.Image(self.WIDTH + 1, self.HEIGHT, self.CHANNELS,
                      bytearray([50] * ((self.WIDTH + 1) * self.HEIGHT * self.CHANNELS)))
        result = png.diff(a, b)
        self.assertTrue(result.size_mismatch)
        self.assertFalse(result.within(1.0))  # even a lax threshold can't save it


class DiffArtifact(unittest.TestCase):
    def test_changed_pixel_is_red_unchanged_is_grey(self):
        a = png.Image(2, 2, 3, bytearray([
            10, 20, 30,     # (0,0) — will change
            40, 50, 60,     # (1,0) — unchanged
            5, 5, 5,        # (0,1) — unchanged
            100, 110, 120,  # (1,1) — unchanged
        ]))
        b = png.Image(2, 2, 3, bytearray([
            200, 200, 200,
            40, 50, 60,
            5, 5, 5,
            100, 110, 120,
        ]))
        artifact = png.diff_artifact(a, b)
        self.assertEqual(artifact.pixel(0, 0), (255, 0, 0))
        for x, y in ((1, 0), (0, 1), (1, 1)):
            r, g, bl = artifact.pixel(x, y)
            self.assertEqual(r, g)
            self.assertEqual(g, bl)


class ScanScript(unittest.TestCase):
    def test_javascript_dependencies(self):
        body = """
        document.getElementById('a');
        document.querySelector('.x');
        document.querySelectorAll('#y .z');
        el.addEventListener('click', fn);
        el.classList.add('open');
        const cls = '.fb-1a2b3c';
        """
        result = baseline.scan_script("s1", "javascript", body)
        touches = result["touches"]
        self.assertIn("a", touches["ids"])
        self.assertIn(".x", touches["selectors"])
        self.assertIn("#y .z", touches["selectors"])
        self.assertIn("click", touches["events"])
        self.assertIn("open", touches["class_ops"])
        self.assertIn("fb-1a2b3c", touches["minted_classes"])

    def test_css_dependencies_ignore_declarations(self):
        body = "/* c */ .menu-card, #hero { color: red } .fb-abc123 span { x }"
        result = baseline.scan_script("s2", "css", body)
        touches = result["touches"]
        self.assertIn("menu-card", touches["classes"])
        self.assertIn("fb-abc123", touches["classes"])
        self.assertIn("hero", touches["ids"])
        self.assertIn("fb-abc123", touches["minted_classes"])
        dumped = json.dumps(touches)
        self.assertNotIn("red", dumped)

    def test_scan_scripts_aggregates_minted_classes(self):
        records = [
            {"name": "s1", "script_type": "javascript",
             "script": "classList.add('open'); '.fb-aaaa11'"},
            {"name": "s2", "script_type": "css",
             "script": ".bldr-bbbb22 { color: blue }"},
        ]
        result = baseline.scan_scripts(records)
        self.assertEqual(result["_meta"]["scripts"], 2)
        self.assertEqual(len(result["scripts"]), 2)
        self.assertIn("fb-aaaa11", result["minted_classes_all"])
        self.assertIn("bldr-bbbb22", result["minted_classes_all"])


class ShotName(unittest.TestCase):
    def test_empty_route_becomes_home(self):
        self.assertEqual(shots_mod.shot_name("", "1280"), "home-1280.png")

    def test_nested_route_flattens_slashes(self):
        self.assertEqual(shots_mod.shot_name("a/b", 576), "a--b-576.png")


# -- run_oracle: injected shooter, no playwright, no network -----------------

_SHOT_NAME = "menu-100.png"


def _base_image() -> png.Image:
    return png.Image(6, 4, 3, _varied_pixels(6, 4, 3))


def _shift_image(img: png.Image, lo: int, hi: int) -> png.Image:
    """Copy `img` with bytes [lo:hi) shifted by 128 mod 256 — a change well
    beyond any plausible tolerance, over a real chunk of the image."""
    pixels = bytearray(img.pixels)
    for i in range(lo, hi):
        pixels[i] = (pixels[i] + 128) % 256
    return png.Image(img.width, img.height, img.channels, pixels)


def _manifest(clone_url: str = "http://example.test") -> dict:
    return {
        "site": "acme",
        "clone_url": clone_url,
        "created_utc": "2026-01-01T00:00:00+00:00",
        "viewports": [[100, 80]],
        "routes_captured": ["menu"],
        "shots": [_SHOT_NAME],
    }


def _write_baseline(base_dir: Path, manifest: dict, image: png.Image) -> None:
    base_dir.mkdir(parents=True, exist_ok=True)
    (base_dir / "manifest.json").write_text(json.dumps(manifest))
    shots_dir = base_dir / "shots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    for name in manifest["shots"]:
        (shots_dir / name).write_bytes(png.encode(image))


def _make_shooter(after_image: png.Image):
    def shooter(url, routes, out_dir, *, viewports=None):
        out_path = Path(out_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        (out_path / _SHOT_NAME).write_bytes(png.encode(after_image))
        return {}
    return shooter


class RunOracle(unittest.TestCase):
    def test_identical_after_is_ok_and_writes_report(self):
        with tempfile.TemporaryDirectory() as d:
            base_dir = Path(d) / "opt" / "baseline"
            image = _base_image()
            _write_baseline(base_dir, _manifest(), image)

            report = run_oracle("acme", baseline_dir=base_dir,
                                shooter=_make_shooter(image))

            self.assertTrue(report["ok"])
            self.assertEqual(report["failed"], 0)
            pair = report["pairs"][0]
            self.assertTrue(pair["ok"])
            self.assertEqual(pair["differing"], 0)
            self.assertNotIn("artifact", pair)
            self.assertNotIn("size_mismatch", pair)

            report_path = base_dir.parent / "oracle" / "report.json"
            self.assertTrue(report_path.exists())
            self.assertEqual(json.loads(report_path.read_text()), report)

    def test_big_changed_region_fails_and_writes_artifact(self):
        with tempfile.TemporaryDirectory() as d:
            base_dir = Path(d) / "opt" / "baseline"
            image = _base_image()
            _write_baseline(base_dir, _manifest(), image)
            total_bytes = image.width * image.height * image.channels
            changed = _shift_image(image, 0, total_bytes // 2)

            report = run_oracle("acme", baseline_dir=base_dir,
                                shooter=_make_shooter(changed))

            self.assertFalse(report["ok"])
            self.assertEqual(report["failed"], 1)
            pair = report["pairs"][0]
            self.assertFalse(pair["ok"])
            self.assertIn("artifact", pair)
            self.assertTrue(Path(pair["artifact"]).exists())

    def test_missing_baseline_manifest_raises_cannot_check(self):
        with tempfile.TemporaryDirectory() as d:
            base_dir = Path(d) / "opt" / "baseline"
            base_dir.mkdir(parents=True)  # no manifest.json written
            with self.assertRaises(CannotCheck):
                run_oracle("acme", baseline_dir=base_dir)

    def test_dimension_mismatch_fails_without_artifact(self):
        with tempfile.TemporaryDirectory() as d:
            base_dir = Path(d) / "opt" / "baseline"
            image = _base_image()
            _write_baseline(base_dir, _manifest(), image)
            different_size = png.Image(
                image.width, image.height + 1, image.channels,
                _varied_pixels(image.width, image.height + 1, image.channels))

            report = run_oracle("acme", baseline_dir=base_dir,
                                shooter=_make_shooter(different_size))

            self.assertFalse(report["ok"])
            pair = report["pairs"][0]
            self.assertFalse(pair["ok"])
            self.assertTrue(pair.get("size_mismatch"))
            self.assertNotIn("artifact", pair)
            artifact_path = base_dir.parent / "oracle" / f"diff-{_SHOT_NAME}"
            self.assertFalse(artifact_path.exists())


# -- tokenize: colour mining, proposals, rewrite, apply-time checks ---------
#
# Pure functions plus tempdir-backed I/O. `tokenize.ROOT` is monkeypatched
# (save/restore, mirroring test_audit.py's pattern) wherever file layout
# under a site directory matters. No network, no bench, no playwright: the
# HTTP fetch in check_resolution is exercised through its `opener` injection
# point, exactly as run_oracle is exercised through `shooter` above.

def _write_proposals(root: Path, site: str, proposals: list[dict]) -> None:
    path = root / "sites" / site / "opt" / "proposals" / "tokens.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"_meta": {}, "proposals": proposals}))


class _FakeHttpResponse:
    """Minimal stand-in for the object `urllib.request.urlopen` returns:
    a context manager whose `.read()` yields raw bytes."""

    def __init__(self, data: bytes):
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._data


def _fake_opener(css: str):
    def opener(request, timeout=15):
        return _FakeHttpResponse(css.encode("utf-8"))
    return opener


def _raising_opener(request, timeout=15):
    raise OSError("connection refused")


class NormColour(unittest.TestCase):
    def test_three_digit_expands_and_lowercases(self):
        self.assertEqual(tokenize.norm("#ABC"), "#aabbcc")

    def test_six_digit_lowercased(self):
        self.assertEqual(tokenize.norm("#AABBCC"), "#aabbcc")

    def test_eight_digit_passes_through_lowercased(self):
        self.assertEqual(tokenize.norm("#AABBCCDD"), "#aabbccdd")


class MineColours(unittest.TestCase):
    def test_colour_prop_counted_non_colour_prop_and_content_ignored(self):
        trees = {
            "page:home": [
                {
                    "blockId": "b1",
                    "baseStyles": {"color": "#ABC", "width": "#ABC"},
                    "innerHTML": "some text mentioning #ABC in content",
                    "children": [
                        {"blockId": "b2",
                         "baseStyles": {"backgroundColor": "#aabbcc"}},
                    ],
                },
            ],
            "component:foo": [
                {"blockId": "c1", "baseStyles": {"borderColor": "#AABBCC"}},
            ],
        }

        mined = tokenize.mine_colours(trees)

        self.assertIn("#aabbcc", mined)
        # color(#ABC) + backgroundColor(#aabbcc) + borderColor(#AABBCC) == 3;
        # width(#ABC) and innerHTML are never counted.
        self.assertEqual(mined["#aabbcc"]["occurrences"], 3)
        self.assertEqual(mined["#aabbcc"]["where"],
                         {"page:home", "component:foo"})
        # No stray entries from any other normalization of the same colour.
        self.assertEqual(len(mined), 1)


class BuildProposals(unittest.TestCase):
    def test_sorted_by_occurrences_descending(self):
        mined = {
            "#111111": {"occurrences": 1, "where": {"page:a"}},
            "#222222": {"occurrences": 5, "where": {"page:b"}},
            "#333333": {"occurrences": 3, "where": {"page:c"}},
        }
        result = tokenize.build_proposals(mined, site="acme")
        values = [p["value"] for p in result["proposals"]]
        self.assertEqual(values, ["#222222", "#333333", "#111111"])
        for p in result["proposals"]:
            self.assertEqual(p["name"], "")
            self.assertEqual(p["status"], "proposed")

    def test_rerun_preserves_human_edits_and_drops_vanished_colour(self):
        existing = {
            "_meta": {},
            "proposals": [
                {"value": "#222222", "occurrences": 5, "where": ["page:b"],
                 "name": "Brand Blue", "status": "accepted"},
                {"value": "#999999", "occurrences": 1, "where": ["page:z"],
                 "name": "Ghost", "status": "accepted"},
            ],
        }
        # #999999 no longer mined; #111111 is newly mined and has no prior
        # human decision.
        mined = {
            "#111111": {"occurrences": 1, "where": {"page:a"}},
            "#222222": {"occurrences": 5, "where": {"page:b"}},
        }

        result = tokenize.build_proposals(mined, site="acme", existing=existing)
        by_value = {p["value"]: p for p in result["proposals"]}

        self.assertEqual(by_value["#222222"]["name"], "Brand Blue")
        self.assertEqual(by_value["#222222"]["status"], "accepted")
        self.assertEqual(by_value["#111111"]["name"], "")
        self.assertEqual(by_value["#111111"]["status"], "proposed")
        self.assertNotIn("#999999", by_value)


class RewriteTree(unittest.TestCase):
    def setUp(self):
        self.roots = [
            {
                "blockId": "b1",
                "baseStyles": {
                    "color": "#aabbcc",
                    "outlineColor": "#ffffff",
                    "width": "#aabbcc",
                },
                "children": [
                    {"blockId": "b2",
                     "baseStyles": {
                         "borderColor": "var(--already-there, #aabbcc)",
                     }},
                ],
            },
        ]
        self.mapping = {"#aabbcc": "uuid-123"}
        self.before_ids = tokenize.block_ids(self.roots)
        self.replaced = tokenize.rewrite_tree(self.roots, self.mapping)

    def test_mapped_colour_in_colour_prop_becomes_var_and_counts(self):
        self.assertEqual(self.replaced, 1)
        self.assertEqual(self.roots[0]["baseStyles"]["color"],
                         "var(--uuid-123, #aabbcc)")

    def test_unmapped_colour_untouched(self):
        self.assertEqual(self.roots[0]["baseStyles"]["outlineColor"],
                         "#ffffff")

    def test_value_already_containing_var_untouched(self):
        self.assertEqual(
            self.roots[0]["children"][0]["baseStyles"]["borderColor"],
            "var(--already-there, #aabbcc)")

    def test_colour_inside_non_colour_prop_untouched(self):
        self.assertEqual(self.roots[0]["baseStyles"]["width"], "#aabbcc")

    def test_block_ids_unchanged_after_rewrite(self):
        self.assertEqual(tokenize.block_ids(self.roots), self.before_ids)


class AcceptedMapping(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)
        old_root = tokenize.ROOT
        tokenize.ROOT = self.root
        self.addCleanup(lambda: setattr(tokenize, "ROOT", old_root))

    def test_accepted_and_named_is_returned(self):
        _write_proposals(self.root, "acme", [
            {"value": "#aabbcc", "occurrences": 3, "where": [],
             "name": "Brand", "status": "accepted"},
        ])
        mapping = tokenize.accepted_mapping("acme")
        self.assertEqual(set(mapping), {"#aabbcc"})
        self.assertEqual(mapping["#aabbcc"]["name"], "Brand")

    def test_accepted_but_unnamed_raises_mentioning_it(self):
        _write_proposals(self.root, "acme", [
            {"value": "#aabbcc", "occurrences": 3, "where": [],
             "name": "", "status": "accepted"},
        ])
        with self.assertRaises(SystemExit) as cm:
            tokenize.accepted_mapping("acme")
        self.assertIn("#aabbcc", str(cm.exception))

    def test_duplicate_names_raise(self):
        _write_proposals(self.root, "acme", [
            {"value": "#aaaaaa", "occurrences": 3, "where": [],
             "name": "Dup", "status": "accepted"},
            {"value": "#bbbbbb", "occurrences": 2, "where": [],
             "name": "Dup", "status": "accepted"},
        ])
        with self.assertRaises(SystemExit) as cm:
            tokenize.accepted_mapping("acme")
        self.assertIn("Dup", str(cm.exception))

    def test_proposed_status_excluded(self):
        _write_proposals(self.root, "acme", [
            {"value": "#aaaaaa", "occurrences": 3, "where": [],
             "name": "Named", "status": "proposed"},
        ])
        mapping = tokenize.accepted_mapping("acme")
        self.assertEqual(mapping, {})


class CheckResolution(unittest.TestCase):
    def test_present_uuids_return_empty_list(self):
        css = ":root { --uuid1: #fff; --uuid2: #000; }"
        missing = tokenize.check_resolution(
            "http://example.test", ["uuid1", "uuid2"],
            opener=_fake_opener(css))
        self.assertEqual(missing, [])

    def test_absent_uuid_is_listed(self):
        css = ":root { --uuid1: #fff; }"
        missing = tokenize.check_resolution(
            "http://example.test", ["uuid1", "uuid2"],
            opener=_fake_opener(css))
        self.assertEqual(missing, ["uuid2"])

    def test_opener_oserror_raises_system_exit(self):
        with self.assertRaises(SystemExit):
            tokenize.check_resolution(
                "http://example.test", ["uuid1"], opener=_raising_opener)


class LoadState(unittest.TestCase):
    def test_string_encoded_blocks_and_dict_block_both_parse_to_lists(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            old_root = tokenize.ROOT
            tokenize.ROOT = root
            try:
                state = root / "sites" / "acme" / "opt" / "baseline" / "state"
                (state / "pages").mkdir(parents=True)
                (state / "components").mkdir(parents=True)

                page_blocks = [{"blockId": "b1"}, {"blockId": "b2"}]
                (state / "pages" / "home.json").write_text(json.dumps({
                    "name": "home", "route": "",
                    "blocks": json.dumps(page_blocks),
                }))

                (state / "components" / "comp1.json").write_text(json.dumps({
                    "component_id": "comp1",
                    "block": {"blockId": "c1"},
                }))

                pages, components = tokenize.load_state("acme")
            finally:
                tokenize.ROOT = old_root

        self.assertEqual(pages["home"], page_blocks)
        self.assertTrue(all(isinstance(b, dict) for b in pages["home"]))

        self.assertEqual(components["comp1"], [{"blockId": "c1"}])
        self.assertTrue(all(isinstance(b, dict) for b in components["comp1"]))


class EnsureVariablesCollisions(unittest.TestCase):
    """`ensure_variables` mints via an injected `runner` (the bench script is
    never actually run) — a proposal carrying a `uuid` updates that record;
    one without a `uuid` must mint fresh, and the bench script reports back
    any name collision with a foreign Builder Variable for the tool to
    refuse rather than silently adopt (TRAP-007)."""

    def test_proposal_with_uuid_updates_and_maps_by_colour(self):
        accepted = {
            "#aabbcc": {"value": "#aabbcc", "name": "brand-blue",
                       "status": "accepted", "uuid": "uuid-existing-1"},
        }
        seen_script = {}

        def runner(script):
            seen_script["script"] = script
            return json.dumps({"minted": {"brand-blue": "uuid-existing-1"},
                               "collisions": []})

        mapping = tokenize.ensure_variables("acme", accepted, runner=runner)

        # the uuid the proposal already carried travelled into the script's
        # embedded payload, proving the update-by-uuid path was requested
        self.assertIn("uuid-existing-1", seen_script["script"])
        self.assertIn("uuid", seen_script["script"])
        self.assertEqual(mapping, {"#aabbcc": "uuid-existing-1"})

    def test_collision_refuses_mentioning_name_and_refused(self):
        accepted = {
            "#ffffff": {"value": "#ffffff", "name": "ink", "status": "accepted"},
        }

        def runner(script):
            return json.dumps({"minted": {}, "collisions": ["ink"]})

        with self.assertRaises(SystemExit) as cm:
            tokenize.ensure_variables("acme", accepted, runner=runner)
        message = str(cm.exception)
        self.assertIn("ink", message)
        self.assertIn("REFUSED", message)


class RefuseStaleCheckpoint(unittest.TestCase):
    """`_refuse_stale_checkpoint` compares the baseline manifest's recorded
    `content_hash` against a freshly computed one. `capture_dev.read_state`
    and `capture_dev._content_hash` are monkeypatched so no bench/REST call
    is ever made; `tokenize.ROOT` points at a tempdir carrying only the
    manifest this check reads."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        old_root = tokenize.ROOT
        tokenize.ROOT = self.root
        self.addCleanup(lambda: setattr(tokenize, "ROOT", old_root))

        old_read_state = capture_dev.read_state
        old_content_hash = capture_dev._content_hash
        self.addCleanup(lambda: setattr(capture_dev, "read_state", old_read_state))
        self.addCleanup(lambda: setattr(capture_dev, "_content_hash", old_content_hash))

    def _write_manifest(self, site: str, content_hash: str) -> None:
        state = self.root / "sites" / site / "opt" / "baseline" / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "manifest.json").write_text(
            json.dumps({"content_hash": content_hash}))

    def test_matching_hash_returns_none(self):
        self._write_manifest("acme", "aaa111aaa111")
        capture_dev.read_state = lambda target: {"sentinel": True}
        capture_dev._content_hash = lambda state: "aaa111aaa111"

        self.assertIsNone(tokenize._refuse_stale_checkpoint("acme"))

    def test_different_hash_raises_refused(self):
        self._write_manifest("acme", "aaa111aaa111")
        capture_dev.read_state = lambda target: {"sentinel": True}
        capture_dev._content_hash = lambda state: "bbb222bbb222"

        with self.assertRaises(SystemExit) as cm:
            tokenize._refuse_stale_checkpoint("acme")
        self.assertIn("REFUSED", str(cm.exception))

    def test_the_refusal_names_the_safe_recovery_from_an_interrupted_apply(self):
        # #18: re-baselining straight over a killed-mid-run apply would
        # bless the broken half-apply as the new reference forever. The
        # message has to say "oracle first", not just "re-run baseline".
        self._write_manifest("acme", "aaa111aaa111")
        capture_dev.read_state = lambda target: {"sentinel": True}
        capture_dev._content_hash = lambda state: "bbb222bbb222"

        with self.assertRaises(SystemExit) as cm:
            tokenize._refuse_stale_checkpoint("acme")
        message = str(cm.exception)
        self.assertIn("interrupted", message)
        self.assertIn("optimize oracle", message)
        self.assertIn("do NOT re-baseline yet", message)

    def test_the_force_advice_is_conditioned_on_the_oracle_verdict(self):
        # Review on #18: the first draft told the operator to run
        # `baseline --force` unconditionally, even in the branch where the
        # oracle it just told them to run had failed — the two halves of
        # the same paragraph contradicted each other. And it called --force
        # required when a passing oracle already clears the gate on its
        # own (gates.record_oracle), so the happy path never needs it.
        self._write_manifest("acme", "aaa111aaa111")
        capture_dev.read_state = lambda target: {"sentinel": True}
        capture_dev._content_hash = lambda state: "bbb222bbb222"

        with self.assertRaises(SystemExit) as cm:
            tokenize._refuse_stale_checkpoint("acme")
        message = str(cm.exception)
        self.assertIn("If it passes", message)
        self.assertIn("no --force", message)
        self.assertIn("If it fails", message)

    def test_a_dry_run_does_not_get_the_apply_time_recovery_advice(self):
        # advise_recovery=False (collapse.py's dry-run path): the
        # interrupted-apply/re-apply paragraph assumes an apply refusal and
        # reads as nonsense in a warning that never refused anything —
        # and folding a multi-paragraph message into one `WARNING: {exc}`
        # line would garble it regardless (#18 review).
        self._write_manifest("acme", "aaa111aaa111")
        capture_dev.read_state = lambda target: {"sentinel": True}
        capture_dev._content_hash = lambda state: "bbb222bbb222"

        with self.assertRaises(SystemExit) as cm:
            tokenize._refuse_stale_checkpoint("acme", advise_recovery=False)
        message = str(cm.exception)
        self.assertIn("REFUSED", message)
        self.assertNotIn("interrupted", message)
        self.assertNotIn("\n\n", message)

    def test_missing_manifest_raises_mentioning_baseline(self):
        # No manifest.json written at all for this site.
        with self.assertRaises(SystemExit) as cm:
            tokenize._refuse_stale_checkpoint("acme")
        self.assertIn("baseline", str(cm.exception))


# -- fonts: stack mining, proposals, rewrite, load-proof checks -------------
#
# Same skeleton as tokenize above: pure functions plus tempdir-backed I/O.
# `mine()` calls into tokenize.load_state/_select, so both fonts.ROOT (used
# by fonts.proposal_path) and tokenize.ROOT (used by load_state/_select for
# the baseline state directory) are monkeypatched together wherever `mine()`
# is exercised. check_loads is exercised through its `opener` injection
# point, reusing the fake-HTTP-response plumbing from CheckResolution above.
# No network, no bench, no playwright.

def _write_font_proposals(root: Path, site: str, proposals: list[dict]) -> None:
    path = root / "sites" / site / "opt" / "proposals" / "fonts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"_meta": {}, "proposals": proposals}))


class PrimaryFamily(unittest.TestCase):
    def test_multi_family_stack_takes_first_unquoted(self):
        self.assertEqual(fonts.primary_family("Merriline, ui-sans-serif"),
                         "Merriline")

    def test_single_quoted_first_family_is_unquoted(self):
        self.assertEqual(fonts.primary_family("'Skybald', cursive"),
                         "Skybald")

    def test_single_family_stack_returned_as_is(self):
        self.assertEqual(fonts.primary_family("Inter"), "Inter")


class MineFonts(unittest.TestCase):
    def test_counts_across_style_keys_ignores_styleless_aggregates_where(self):
        trees = {
            "page:home": [
                {
                    "blockId": "b1",
                    "baseStyles": {"fontFamily": "Merriline, ui-sans-serif"},
                    "mobileStyles": {"fontFamily": "Merriline, ui-sans-serif"},
                    "innerHTML": "mentions Merriline, ui-sans-serif in content",
                    "children": [
                        {"blockId": "b2"},  # no style dicts at all
                        {"blockId": "b3", "baseStyles": {"color": "#fff"}},
                    ],
                },
            ],
            "component:foo": [
                {"blockId": "c1",
                 "tabletStyles": {"fontFamily": "Merriline, ui-sans-serif"}},
            ],
        }

        mined = fonts.mine_fonts(trees)

        self.assertIn("Merriline, ui-sans-serif", mined)
        entry = mined["Merriline, ui-sans-serif"]
        # baseStyles(b1) + mobileStyles(b1) + tabletStyles(c1) == 3;
        # b2 (no styles) and b3 (styles without fontFamily) never counted.
        self.assertEqual(entry["occurrences"], 3)
        self.assertEqual(entry["where"], {"page:home", "component:foo"})
        self.assertEqual(len(mined), 1)


class Mine(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        old_fonts_root = fonts.ROOT
        old_tokenize_root = tokenize.ROOT
        fonts.ROOT = self.root
        tokenize.ROOT = self.root
        self.addCleanup(lambda: setattr(fonts, "ROOT", old_fonts_root))
        self.addCleanup(lambda: setattr(tokenize, "ROOT", old_tokenize_root))

    def _write_state(self, site: str) -> None:
        state = self.root / "sites" / site / "opt" / "baseline" / "state"
        (state / "pages").mkdir(parents=True)
        (state / "components").mkdir(parents=True)
        page_blocks = [
            {"blockId": "b1", "baseStyles": {"fontFamily": "Merriline, ui-sans-serif"}},
            {"blockId": "b2", "baseStyles": {"fontFamily": "Merriline, ui-sans-serif"}},
            {"blockId": "b3", "baseStyles": {"fontFamily": "Inter"}},
        ]
        (state / "pages" / "home.json").write_text(json.dumps({
            "name": "home", "route": "",
            "blocks": json.dumps(page_blocks),
        }))

    def test_multi_family_proposed_single_family_marked_single(self):
        self._write_state("acme")

        data = fonts.mine("acme")

        by_stack = {p["stack"]: p for p in data["proposals"]}
        multi = by_stack["Merriline, ui-sans-serif"]
        self.assertEqual(multi["status"], "proposed")
        self.assertEqual(multi["primary"], "Merriline")
        self.assertEqual(multi["occurrences"], 2)
        single = by_stack["Inter"]
        self.assertEqual(single["status"], "single")
        self.assertEqual(single["primary"], "Inter")
        self.assertEqual(single["occurrences"], 1)
        # sorted by occurrences descending
        self.assertEqual([p["stack"] for p in data["proposals"]],
                         ["Merriline, ui-sans-serif", "Inter"])

        path = fonts.proposal_path("acme")
        self.assertTrue(path.exists())
        self.assertEqual(json.loads(path.read_text()), data)

    def test_rerun_preserves_human_edited_primary_and_status(self):
        self._write_state("acme")
        fonts.mine("acme")

        path = fonts.proposal_path("acme")
        saved = json.loads(path.read_text())
        for p in saved["proposals"]:
            if p["stack"] == "Merriline, ui-sans-serif":
                p["primary"] = "Merriline Custom"
                p["status"] = "accepted"
        path.write_text(json.dumps(saved))

        data = fonts.mine("acme")

        by_stack = {p["stack"]: p for p in data["proposals"]}
        self.assertEqual(by_stack["Merriline, ui-sans-serif"]["primary"],
                         "Merriline Custom")
        self.assertEqual(by_stack["Merriline, ui-sans-serif"]["status"],
                         "accepted")
        # untouched single-family entry keeps its status too
        self.assertEqual(by_stack["Inter"]["status"], "single")


class RewriteFonts(unittest.TestCase):
    def setUp(self):
        self.roots = [
            {
                "blockId": "b1",
                "baseStyles": {
                    "fontFamily": "Merriline, ui-sans-serif",
                    "color": "#fff",
                },
                "children": [
                    {"blockId": "b2",
                     "mobileStyles": {"fontFamily": "Inter"}},
                ],
            },
        ]
        self.reductions = {"Merriline, ui-sans-serif": "Merriline"}
        self.before_ids = fonts.block_ids(self.roots)
        self.replaced = fonts.rewrite_fonts(self.roots, self.reductions)

    def test_accepted_stack_replaced_with_primary_and_counted(self):
        self.assertEqual(self.replaced, 1)
        self.assertEqual(self.roots[0]["baseStyles"]["fontFamily"], "Merriline")

    def test_non_matching_stack_untouched(self):
        self.assertEqual(
            self.roots[0]["children"][0]["mobileStyles"]["fontFamily"],
            "Inter")

    def test_block_ids_unchanged_after_rewrite(self):
        self.assertEqual(fonts.block_ids(self.roots), self.before_ids)


class AcceptedReductions(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)
        old_root = fonts.ROOT
        fonts.ROOT = self.root
        self.addCleanup(lambda: setattr(fonts, "ROOT", old_root))

    def test_accepted_entries_returned_single_and_proposed_excluded(self):
        _write_font_proposals(self.root, "acme", [
            {"stack": "Merriline, ui-sans-serif", "primary": "Merriline",
             "occurrences": 2, "where": [], "status": "accepted"},
            {"stack": "Inter", "primary": "Inter",
             "occurrences": 1, "where": [], "status": "single"},
            {"stack": "Roboto, sans-serif", "primary": "Roboto",
             "occurrences": 1, "where": [], "status": "proposed"},
        ])

        reductions = fonts.accepted_reductions("acme")

        self.assertEqual(reductions, {"Merriline, ui-sans-serif": "Merriline"})

    def test_accepted_with_empty_primary_raises(self):
        _write_font_proposals(self.root, "acme", [
            {"stack": "Merriline, ui-sans-serif", "primary": "",
             "occurrences": 2, "where": [], "status": "accepted"},
        ])

        with self.assertRaises(SystemExit) as cm:
            fonts.accepted_reductions("acme")
        self.assertIn("Merriline, ui-sans-serif", str(cm.exception))


class CheckLoads(unittest.TestCase):
    _HTML = """
    <html><head>
    <link rel="stylesheet"
          href="https://fonts.googleapis.com/css2?family=Merriline:wght@400;700&display=swap">
    <style>
    @font-face { font-family: 'Skybald'; src: url(/fonts/skybald.woff2) format('woff2'); }
    </style>
    </head>
    <body>
    <div style="font-family: 'Secondary Font', sans-serif;">Hi</div>
    </body></html>
    """

    def test_google_fonts_url_proves_family(self):
        problems = fonts.check_loads(
            "http://example.test", [""], ["Merriline"],
            opener=_fake_opener(self._HTML))
        self.assertEqual(problems, [])

    def test_style_attribute_only_mention_is_reported_missing(self):
        problems = fonts.check_loads(
            "http://example.test", [""], ["Secondary Font"],
            opener=_fake_opener(self._HTML))
        self.assertEqual(problems, ["/: Secondary Font"])

    def test_font_face_block_proves_family(self):
        problems = fonts.check_loads(
            "http://example.test", [""], ["Skybald"],
            opener=_fake_opener(self._HTML))
        self.assertEqual(problems, [])

    def test_all_three_families_together(self):
        problems = fonts.check_loads(
            "http://example.test", [""],
            ["Merriline", "Secondary Font", "Skybald"],
            opener=_fake_opener(self._HTML))
        self.assertEqual(problems, ["/: Secondary Font"])

    def test_opener_oserror_raises_system_exit(self):
        with self.assertRaises(SystemExit):
            fonts.check_loads(
                "http://example.test", [""], ["Merriline"],
                opener=_raising_opener)


# -- collapse: removable() disqualifiers, collapse_tree() merge passes,
#    _protected_names(), and run()'s tempdir-backed I/O ---------------------
#
# Same skeleton as tokenize/fonts above: pure functions plus tempdir-backed
# I/O. collapse.ROOT and tokenize.ROOT are monkeypatched together wherever
# run() is exercised (mirroring Mine's dual-ROOT setup above), since run()
# sources scripts-scan.json via the former and load_state/_select via the
# latter. No network, no bench, no playwright: apply=True is exercised
# through an injected `runner`; the staleness guard's own bench calls are
# monkeypatched exactly as in RefuseStaleCheckpoint.

def _leaf(block_id: str, element: str = "div") -> dict:
    return {"blockId": block_id, "element": element}


def _bare_wrapper(block_id: str, child: dict, *, classes=None) -> dict:
    return {
        "blockId": block_id,
        "element": "div",
        "classes": list(classes) if classes is not None else ["fb-1a2b3c"],
        "children": [child],
    }


class Removable(unittest.TestCase):
    def test_bare_single_child_wrapper_with_minted_class_is_removable(self):
        block = _bare_wrapper("w1", _leaf("c1"))
        self.assertNotEqual(collapse.removable(block, protected=set()), "")

    def test_two_children_not_removable(self):
        block = {"blockId": "w1", "element": "div", "classes": ["fb-1a2b3c"],
                 "children": [_leaf("c1"), _leaf("c2")]}
        self.assertEqual(collapse.removable(block, protected=set()), "")

    def test_inner_html_not_removable(self):
        block = _bare_wrapper("w1", _leaf("c1"))
        block["innerHTML"] = "<b>hi</b>"
        self.assertEqual(collapse.removable(block, protected=set()), "")

    def test_any_style_dict_non_empty_not_removable(self):
        for key in tokenize.STYLE_KEYS:
            block = _bare_wrapper("w1", _leaf("c1"))
            block[key] = {"color": "#fff"}
            self.assertEqual(collapse.removable(block, protected=set()), "",
                             f"{key} should disqualify")

    def test_named_non_minted_class_not_removable(self):
        block = _bare_wrapper("w1", _leaf("c1"),
                              classes=["fb-1a2b3c", "menu-card"])
        self.assertEqual(collapse.removable(block, protected=set()), "")

    def test_custom_attributes_not_removable(self):
        block = _bare_wrapper("w1", _leaf("c1"))
        block["customAttributes"] = {"data-foo": "bar"}
        self.assertEqual(collapse.removable(block, protected=set()), "")

    def test_other_attribute_not_removable(self):
        block = _bare_wrapper("w1", _leaf("c1"))
        block["attributes"] = {"id": "x"}
        self.assertEqual(collapse.removable(block, protected=set()), "")

    def test_non_div_span_element_not_removable(self):
        block = _bare_wrapper("w1", _leaf("c1"))
        block["element"] = "section"
        self.assertEqual(collapse.removable(block, protected=set()), "")

    def test_child_of_different_tag_not_removable(self):
        # promoting a different tag would shift :nth-of-type/:first-child
        # matches among the wrapper's siblings
        block = _bare_wrapper("w1", _leaf("c1", element="p"))
        self.assertEqual(collapse.removable(block, protected=set()), "")

    def test_extended_from_component_not_removable(self):
        block = _bare_wrapper("w1", _leaf("c1"))
        block["extendedFromComponent"] = "comp1"
        self.assertEqual(collapse.removable(block, protected=set()), "")

    def test_repeater_block_not_removable(self):
        block = _bare_wrapper("w1", _leaf("c1"))
        block["isRepeaterBlock"] = True
        self.assertEqual(collapse.removable(block, protected=set()), "")

    def test_minted_class_in_protected_not_removable(self):
        block = _bare_wrapper("w1", _leaf("c1"), classes=["fb-1a2b3c"])
        self.assertEqual(
            collapse.removable(block, protected={"fb-1a2b3c"}), "")

    def test_block_id_in_protected_not_removable(self):
        block = _bare_wrapper("w1", _leaf("c1"))
        self.assertEqual(collapse.removable(block, protected={"w1"}), "")


class ProtectedNames(unittest.TestCase):
    def test_extracts_from_all_touch_kinds_and_selector_tokens(self):
        scan = {"scripts": [
            {"touches": {
                "ids": ["idA"],
                "classes": ["classB"],
                "class_ops": ["opC"],
                "minted_classes": ["fb-1111"],
                "selectors": [".foo", "#bar", ".foo .baz"],
            }},
        ]}
        protected = collapse._protected_names(scan)
        self.assertEqual(
            protected,
            {"idA", "classB", "opC", "fb-1111", "foo", "bar", "baz"})


class CollapseTreeMerging(unittest.TestCase):
    def test_chain_of_two_wrappers_collapses_across_passes(self):
        content = _leaf("content")
        wrapper2 = _bare_wrapper("w2", content, classes=["fb-cccccc"])
        wrapper1 = _bare_wrapper("w1", wrapper2, classes=["fb-bbbbbb"])
        root = {"blockId": "root", "element": "div", "classes": [],
                "children": [wrapper1]}

        log = collapse.collapse_tree([root], protected=set())

        self.assertEqual(len(log), 2)
        self.assertIs(root["children"][0], content)

    def test_log_entries_carry_removed_kept_under_proof(self):
        content = _leaf("content")
        wrapper = _bare_wrapper("w1", content, classes=["fb-aaaaaa"])
        root = {"blockId": "root", "element": "div", "children": [wrapper]}

        log = collapse.collapse_tree([root], protected=set())

        self.assertEqual(len(log), 1)
        entry = log[0]
        self.assertEqual(entry["removed"], "w1")
        self.assertEqual(entry["kept"], "content")
        self.assertEqual(entry["under"], "root")
        self.assertTrue(entry["proof"])

    def test_root_itself_is_never_removed(self):
        leaf = _leaf("leaf")
        root = {"blockId": "root", "element": "div",
                "classes": ["fb-1a2b3c"], "children": [leaf]}

        log = collapse.collapse_tree([root], protected=set())

        self.assertEqual(log, [])
        self.assertEqual(root["children"], [leaf])

    def test_wrapper_under_component_extension_is_not_touched(self):
        leaf = _leaf("leaf")
        wrapper = _bare_wrapper("w1", leaf, classes=["fb-aaaaaa"])
        comp_block = {
            "blockId": "comp1", "element": "div",
            "extendedFromComponent": "someComponent",
            "children": [wrapper],
        }
        root = {"blockId": "root", "element": "div",
                "children": [comp_block]}

        log = collapse.collapse_tree([root], protected=set())

        self.assertEqual(log, [])
        self.assertEqual(root["children"], [comp_block])
        self.assertEqual(comp_block["children"], [wrapper])
        self.assertEqual(wrapper["children"], [leaf])


def _collapsible_page_blocks(page_name: str = "home") -> list[dict]:
    # same-tag rule: only a div-wrapping-div counts as removable
    leaf = {"blockId": f"{page_name}-leaf", "element": "div"}
    wrapper = {"blockId": f"{page_name}-w1", "element": "div",
              "classes": ["fb-1a2b3c"], "children": [leaf]}
    root = {"blockId": f"{page_name}-root", "element": "div",
            "children": [wrapper]}
    return [root]


class RunCollapse(unittest.TestCase):
    """collapse.run() end to end against a tempdir standing in for `sites/`.
    Both collapse.ROOT and tokenize.ROOT are monkeypatched together, since
    run() sources scripts-scan.json via the former and load_state/_select
    via the latter."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        from buildsmith.workflows.optimize import gates

        old_collapse_root = collapse.ROOT
        old_tokenize_root = tokenize.ROOT
        old_gates_root = gates.ROOT
        collapse.ROOT = self.root
        tokenize.ROOT = self.root
        gates.ROOT = self.root  # apply=True writes the gate ledger
        self.addCleanup(lambda: setattr(collapse, "ROOT", old_collapse_root))
        self.addCleanup(lambda: setattr(tokenize, "ROOT", old_tokenize_root))
        self.addCleanup(lambda: setattr(gates, "ROOT", old_gates_root))

        old_read_state = capture_dev.read_state
        old_content_hash = capture_dev._content_hash
        self.addCleanup(
            lambda: setattr(capture_dev, "read_state", old_read_state))
        self.addCleanup(
            lambda: setattr(capture_dev, "_content_hash", old_content_hash))

    def _write_scan(self, site: str, scan: dict | None = None) -> None:
        path = (self.root / "sites" / site / "opt" / "baseline"
               / "scripts-scan.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(scan if scan is not None
                                   else {"scripts": []}))

    def _write_state(self, site: str, page_name: str = "home",
                     route: str = "") -> None:
        state = self.root / "sites" / site / "opt" / "baseline" / "state"
        (state / "pages").mkdir(parents=True, exist_ok=True)
        (state / "components").mkdir(parents=True, exist_ok=True)
        (state / "pages" / f"{page_name}.json").write_text(json.dumps({
            "name": page_name, "route": route,
            "blocks": json.dumps(_collapsible_page_blocks(page_name)),
        }))

    def _write_manifest(self, site: str, content_hash: str) -> None:
        state = self.root / "sites" / site / "opt" / "baseline" / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "manifest.json").write_text(
            json.dumps({"content_hash": content_hash}))

    def test_dry_run_reports_removed_and_writes_transform_no_runner_needed(self):
        self._write_scan("acme")
        self._write_state("acme")

        def _boom(script):
            raise AssertionError("runner must not be invoked when apply=False")

        result = collapse.run("acme", apply=False, runner=_boom)

        self.assertEqual(result["removed"], 1)
        self.assertFalse(result["applied"])
        self.assertEqual(result["targets"], ["page:home"])
        report_path = (self.root / "sites" / "acme" / "opt" / "transforms"
                       / "collapse" / "page-home.json")
        self.assertTrue(report_path.exists())
        report = json.loads(report_path.read_text())
        self.assertEqual(len(report["log"]), 1)

    def test_missing_scripts_scan_raises_mentioning_scan(self):
        with self.assertRaises(SystemExit) as cm:
            collapse.run("acme")
        self.assertIn("scan", str(cm.exception))

    def test_route_filter_matching_nothing_refuses(self):
        self._write_scan("acme")
        self._write_state("acme", route="")
        with self.assertRaises(SystemExit) as cm:
            collapse.run("acme", routes=["/nonexistent"])
        self.assertIn("REFUSED", str(cm.exception))

    def test_apply_true_invokes_runner_with_page_name_and_reports_applied(self):
        self._write_scan("acme")
        self._write_state("acme")
        self._write_manifest("acme", "matching-hash")
        capture_dev.read_state = lambda target: {"sentinel": True}
        capture_dev._content_hash = lambda state: "matching-hash"

        seen_script = {}

        def runner(script):
            seen_script["script"] = script
            return "applied 1"

        result = collapse.run("acme", apply=True, runner=runner)

        self.assertTrue(result["applied"])
        self.assertIn("home", seen_script["script"])

    def test_apply_records_a_pending_gate_entry_before_the_write_back(self):
        # The ledger lives in the library, not the CLI, so no caller can
        # mutate the sandbox without a pending entry — and it is written
        # BEFORE the write-back, so a failed write still shows as unproved.
        from buildsmith.workflows.optimize import gates

        self._write_scan("acme")
        self._write_state("acme")
        self._write_manifest("acme", "matching-hash")
        capture_dev.read_state = lambda target: {"sentinel": True}
        capture_dev._content_hash = lambda state: "matching-hash"

        def failing_runner(script):
            raise RuntimeError("bench fell over mid write-back")

        with self.assertRaises(RuntimeError):
            collapse.run("acme", apply=True, runner=failing_runner)

        open_entries = gates.pending("acme")
        self.assertEqual([e["transform"] for e in open_entries], ["collapse"])
        self.assertEqual(open_entries[0]["baseline_hash"], "matching-hash")


# -- componentize: shape hashing, candidate mining, and mine()'s tempdir-
#    backed I/O -------------------------------------------------------------
#
# shape_of() is pure and exercised directly on hand-built blocks. find_
# candidates() is pure over in-memory trees. mine() calls into tokenize.
# load_state/_select, so both componentize.ROOT (proposal_path) and
# tokenize.ROOT (the state directory) are monkeypatched together, mirroring
# fonts' Mine class above. No network, no bench, no playwright.

def _c_leaf(block_id: str, element: str = "p") -> dict:
    return {"blockId": block_id, "element": element}


def _c_row(prefix: str) -> dict:
    """4-block shape: a div wrapping three <p> leaves."""
    return {
        "blockId": f"{prefix}-row",
        "element": "div",
        "children": [
            _c_leaf(f"{prefix}-p1"),
            _c_leaf(f"{prefix}-p2"),
            _c_leaf(f"{prefix}-p3"),
        ],
    }


def _c_card(prefix: str) -> dict:
    """6-block shape: a div with an <h2> header and a _c_row body — the
    candidate every test in this section expects to survive."""
    return {
        "blockId": f"{prefix}-card",
        "element": "div",
        "children": [
            {"blockId": f"{prefix}-header", "element": "h2"},
            _c_row(prefix),
        ],
    }


def _c_pair(prefix: str) -> dict:
    """2-block shape — below MIN_BLOCKS, must never be reported no matter
    how often it repeats."""
    return {
        "blockId": f"{prefix}-pair",
        "element": "div",
        "children": [{"blockId": f"{prefix}-span", "element": "span"}],
    }


def _c_duo(prefix: str) -> dict:
    """4-block shape, structurally distinct from _c_row (element 'section'
    not 'div') — clears MIN_BLOCKS but is made to occur only twice, below
    MIN_OCCURRENCES."""
    return {
        "blockId": f"{prefix}-duo",
        "element": "section",
        "children": [
            _c_leaf(f"{prefix}-d1"), _c_leaf(f"{prefix}-d2"),
            _c_leaf(f"{prefix}-d3"),
        ],
    }


def _c_footer(prefix: str) -> dict:
    """4-block shape, structurally distinct from every helper above (element
    'footer', children '<a>') — a second, smaller top-level candidate that
    occurs exactly MIN_OCCURRENCES times, used to prove result ordering."""
    return {
        "blockId": f"{prefix}-footer",
        "element": "footer",
        "children": [
            _c_leaf(f"{prefix}-f1", "a"), _c_leaf(f"{prefix}-f2", "a"),
            _c_leaf(f"{prefix}-f3", "a"),
        ],
    }


class ShapeOf(unittest.TestCase):
    def test_same_element_and_style_keys_same_hash_despite_values_and_ids(self):
        a = {"blockId": "b1", "element": "div",
             "baseStyles": {"color": "red"}, "innerHTML": "hello"}
        b = {"blockId": "b2", "element": "div",
             "baseStyles": {"color": "blue"}, "innerHTML": "goodbye"}
        digest_a, count_a = componentize.shape_of(a)
        digest_b, count_b = componentize.shape_of(b)
        self.assertEqual(digest_a, digest_b)
        self.assertEqual(count_a, count_b)

    def test_different_element_different_hash(self):
        a = {"blockId": "b1", "element": "div", "baseStyles": {"color": "red"}}
        b = {"blockId": "b2", "element": "span", "baseStyles": {"color": "blue"}}
        self.assertNotEqual(componentize.shape_of(a)[0],
                            componentize.shape_of(b)[0])

    def test_different_style_key_set_different_hash(self):
        a = {"blockId": "b1", "element": "div",
             "baseStyles": {"color": "red", "width": "10px"}}
        b = {"blockId": "b2", "element": "div",
             "baseStyles": {"color": "blue"}}
        self.assertNotEqual(componentize.shape_of(a)[0],
                            componentize.shape_of(b)[0])

    def test_child_order_matters(self):
        x = {"blockId": "x", "element": "a"}
        y = {"blockId": "y", "element": "b"}
        forward = {"blockId": "p1", "element": "div", "children": [x, y]}
        backward = {"blockId": "p2", "element": "div", "children": [y, x]}
        self.assertNotEqual(componentize.shape_of(forward)[0],
                            componentize.shape_of(backward)[0])

    def test_count_equals_subtree_block_count(self):
        _, count = componentize.shape_of(_c_card("z"))
        self.assertEqual(count, 6)

    def test_named_class_changes_hash_minted_class_does_not(self):
        bare = {"blockId": "b1", "element": "div"}
        named = {"blockId": "b2", "element": "div", "classes": ["menu-card"]}
        minted_fb = {"blockId": "b3", "element": "div",
                     "classes": ["fb-1a2b3c"]}
        minted_bldr = {"blockId": "b4", "element": "div",
                       "classes": ["bldr-xyz999"]}
        base_digest = componentize.shape_of(bare)[0]
        self.assertNotEqual(componentize.shape_of(named)[0], base_digest)
        self.assertEqual(componentize.shape_of(minted_fb)[0], base_digest)
        self.assertEqual(componentize.shape_of(minted_bldr)[0], base_digest)


class FindCandidates(unittest.TestCase):
    """A 6-block card shape repeated 5 times and a 2-block pair repeated 10
    times, spread across five page-like labels, plus a duo shape repeated
    only twice and a footer shape repeated exactly 3 times."""

    def setUp(self):
        self.trees = {}
        for i in range(1, 6):
            label = f"p{i}"
            roots = [_c_card(label), _c_pair(f"{label}a"), _c_pair(f"{label}b")]
            if i <= 2:
                roots.append(_c_duo(label))
            if i <= 3:
                roots.append(_c_footer(label))
            self.trees[label] = roots
        self.result = componentize.find_candidates(self.trees)

    def test_only_card_and_footer_are_reported(self):
        self.assertEqual(len(self.result), 2)
        elements = {c["element"] for c in self.result}
        self.assertEqual(elements, {"div", "footer"})

    def test_small_pair_excluded_by_min_blocks(self):
        # 10 occurrences, well past MIN_OCCURRENCES, but only 2 blocks each
        for c in self.result:
            self.assertNotEqual(c["blocks_per_instance"], 2)

    def test_duo_excluded_by_min_occurrences(self):
        for c in self.result:
            self.assertNotEqual(c["element"], "section")

    def test_card_nested_row_not_separately_reported(self):
        # the row lives inside every card and never occurs outside one, so
        # it must be pruned even though it independently clears both
        # thresholds (4 blocks, 5 occurrences)
        row_digest = componentize.shape_of(_c_row("z"))[0]
        shapes = {c["shape"] for c in self.result}
        self.assertNotIn(row_digest, shapes)

    def test_ordering_largest_total_blocks_first(self):
        card, footer = self.result[0], self.result[1]
        self.assertEqual(card["element"], "div")
        self.assertEqual(card["total_blocks"], 30)
        self.assertEqual(footer["element"], "footer")
        self.assertEqual(footer["total_blocks"], 12)
        self.assertGreater(card["total_blocks"], footer["total_blocks"])

    def test_instance_block_ids_collected(self):
        card = next(c for c in self.result if c["element"] == "div")
        self.assertEqual(card["occurrences"], 5)
        self.assertEqual(
            set(card["instance_block_ids"]),
            {f"p{i}-card" for i in range(1, 6)})

        footer = next(c for c in self.result if c["element"] == "footer")
        self.assertEqual(footer["occurrences"], 3)
        self.assertEqual(
            set(footer["instance_block_ids"]),
            {"p1-footer", "p2-footer", "p3-footer"})


class MineComponentize(unittest.TestCase):
    """componentize.mine()'s tempdir-backed I/O. Both componentize.ROOT
    (proposal_path) and tokenize.ROOT (state directory, used by
    load_state/_select) are monkeypatched together, mirroring fonts' Mine
    class above."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        old_componentize_root = componentize.ROOT
        old_tokenize_root = tokenize.ROOT
        componentize.ROOT = self.root
        tokenize.ROOT = self.root
        self.addCleanup(
            lambda: setattr(componentize, "ROOT", old_componentize_root))
        self.addCleanup(lambda: setattr(tokenize, "ROOT", old_tokenize_root))

    def _write_page(self, site: str, name: str, blocks: list[dict],
                    route: str = "") -> None:
        state = self.root / "sites" / site / "opt" / "baseline" / "state"
        (state / "pages").mkdir(parents=True, exist_ok=True)
        (state / "components").mkdir(parents=True, exist_ok=True)
        (state / "pages" / f"{name}.json").write_text(json.dumps({
            "name": name, "route": route,
            "blocks": json.dumps(blocks),
        }))

    def _write_component(self, site: str, component_id: str,
                         block: dict) -> None:
        state = self.root / "sites" / site / "opt" / "baseline" / "state"
        (state / "components").mkdir(parents=True, exist_ok=True)
        (state / "components" / f"{component_id}.json").write_text(json.dumps({
            "component_id": component_id,
            "block": json.dumps(block),
        }))

    def _page_root(self) -> dict:
        return {
            "blockId": "page-root", "element": "div",
            "children": [_c_card("a"), _c_card("b"), _c_card("c")],
        }

    def test_repeating_candidate_is_proposed(self):
        self._write_page("acme", "home", [self._page_root()])

        data = componentize.mine("acme")

        self.assertEqual(len(data["proposals"]), 1)
        proposal = data["proposals"][0]
        self.assertEqual(proposal["status"], "proposed")
        self.assertEqual(proposal["name"], "")
        self.assertEqual(proposal["occurrences"], 3)
        self.assertEqual(proposal["blocks_per_instance"], 6)

        path = componentize.proposal_path("acme")
        self.assertTrue(path.exists())
        self.assertEqual(json.loads(path.read_text()), data)

    def test_rerun_preserves_human_set_name_and_status(self):
        self._write_page("acme", "home", [self._page_root()])
        componentize.mine("acme")

        path = componentize.proposal_path("acme")
        saved = json.loads(path.read_text())
        saved["proposals"][0]["name"] = "Menu Card"
        saved["proposals"][0]["status"] = "accepted"
        path.write_text(json.dumps(saved))

        data = componentize.mine("acme")

        self.assertEqual(data["proposals"][0]["name"], "Menu Card")
        self.assertEqual(data["proposals"][0]["status"], "accepted")

    def test_component_trees_never_surface_in_proposals(self):
        # The page carries the same repeating card three times (a proposable
        # candidate); the component carries a *different*, equally-repeating
        # shape (three 4-block <aside> widgets) that would also qualify if
        # detection ever looked inside components. It never does: mine()
        # feeds only page: labels to find_candidates.
        self._write_page("acme", "home", [self._page_root()])
        widget_root = {
            "blockId": "comp-root", "element": "div",
            "children": [
                {"blockId": f"widget-{n}", "element": "aside",
                 "children": [_c_leaf(f"widget-{n}-p1"),
                             _c_leaf(f"widget-{n}-p2"),
                             _c_leaf(f"widget-{n}-p3")]}
                for n in range(3)
            ],
        }
        self._write_component("acme", "comp1", widget_root)

        data = componentize.mine("acme")

        self.assertEqual(len(data["proposals"]), 1)
        self.assertEqual(data["proposals"][0]["element"], "div")
        dumped = json.dumps(data)
        self.assertNotIn("widget", dumped)
        self.assertNotIn("comp-root", dumped)


if __name__ == "__main__":
    unittest.main()


class ScriptSourceDiscoveryTest(unittest.TestCase):
    """Collapse refuses without a script scan, so the scan must find scripts
    wherever the site's arrival path put them (ADR-008): live-export records
    for adopted sites, head JS assets for imported clones (TRAP-018 moved
    inline scripts to files — which silently emptied the old scanner)."""

    def test_head_js_assets_are_a_script_source(self):
        import tempfile
        from pathlib import Path

        from buildsmith.workflows.optimize.baseline import collect_script_records
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            (root / "assets" / "example-head-index-0.js").write_text(
                "document.querySelector('.menu-open')")
            records, source = collect_script_records(root)
            self.assertEqual(len(records), 1)
            self.assertIn("head", records[0]["name"])
            self.assertIn("assets/*-head-*.js", source)

    def test_both_sources_combine(self):
        import json
        import tempfile
        from pathlib import Path

        from buildsmith.workflows.optimize.baseline import collect_script_records
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "live-export" / "doctypes").mkdir(parents=True)
            (root / "live-export" / "doctypes" / "builder-client-script.json"
             ).write_text(json.dumps([{"name": "s1", "script_type": "JavaScript",
                                       "script": "x()"}]))
            (root / "assets").mkdir()
            (root / "assets" / "example-head-menu-0.js").write_text("y()")
            records, source = collect_script_records(root)
            self.assertEqual(len(records), 2)
            self.assertIn("live-export", source)
            self.assertIn("head", source)

    def test_no_source_is_empty_and_says_so(self):
        import tempfile
        from pathlib import Path

        from buildsmith.workflows.optimize.baseline import collect_script_records
        with tempfile.TemporaryDirectory() as tmp:
            records, source = collect_script_records(Path(tmp))
            self.assertEqual(records, [])
            self.assertEqual(source, "")
