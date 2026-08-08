"""Tests for W1 — replicate.

The success floor is *all original content present, routes preserved*, so most
of these are about faithfulness: word order, content that must not vanish, and
the difference between "nothing to convert" and "converted to nothing".
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from buildsmith.workflows.replicate import (  # noqa: E402
    ConversionError,
    crawl_local,
    html_to_blocks,
    replicate,
)
from buildsmith.workflows.replicate.crawl import (  # noqa: E402
    CrawlResult,
    crawl_site,
    route_for,
    save_crawl,
)


def texts(blocks):
    """Every innerHTML in document order — the faithfulness question."""
    out = []
    for block in blocks:
        if block.get("innerHTML"):
            out.append(block["innerHTML"])
        out += texts(block.get("children") or [])
    return out


class Faithfulness(unittest.TestCase):
    def test_mixed_content_keeps_word_order(self):
        # Builder emits innerHTML then children, so innerHTML="Some text." with a
        # <b> child renders as "Some text. bold". Verified against the real
        # renderer; the fix is to make every text run an ordered child.
        result = html_to_blocks("<body><p>Some <b>bold</b> text.</p></body>")
        self.assertEqual(texts(result.blocks), ["Some", "bold", "text."])

    def test_text_only_elements_stay_simple(self):
        # Otherwise every heading in a replicated site becomes a span in an h1.
        result = html_to_blocks("<body><h1>Plain</h1></body>")
        self.assertEqual(result.blocks[0]["element"], "h1")
        self.assertEqual(result.blocks[0]["innerHTML"], "Plain")
        self.assertNotIn("children", result.blocks[0])

    def test_links_and_images_keep_their_attributes(self):
        result = html_to_blocks(
            '<body><a href="/about" title="t">About</a><img src="/x.png" alt="X"></body>'
        )
        link, image = result.blocks
        self.assertEqual(link["attributes"]["href"], "/about")
        self.assertEqual(image["attributes"], {"src": "/x.png", "alt": "X"})

    def test_inline_styles_become_camelcase_base_styles(self):
        result = html_to_blocks(
            '<body><div style="background-color:#fff;padding-top:4px"></div></body>')
        self.assertEqual(
            result.blocks[0]["baseStyles"], {"backgroundColor": "#fff", "paddingTop": "4px"}
        )

    def test_classes_are_preserved(self):
        result = html_to_blocks('<body><div class="a b"></div></body>')
        self.assertEqual(result.blocks[0]["classes"], ["a", "b"])

    def test_nesting_survives(self):
        result = html_to_blocks("<body><div><ul><li>One</li><li>Two</li></ul></div></body>")
        items = result.blocks[0]["children"][0]["children"]
        self.assertEqual([i["innerHTML"] for i in items], ["One", "Two"])

    def test_unbalanced_markup_does_not_derail_the_tree(self):
        # The norm in the wild. It must not swallow the rest of the document.
        result = html_to_blocks("<body><div><p>One<div>Two</div></body>")
        self.assertIn("One", texts(result.blocks))
        self.assertIn("Two", texts(result.blocks))


class NothingVanishesSilently(unittest.TestCase):
    def test_a_script_never_becomes_a_block(self):
        # It may be carried as page JS, but it is never content.
        result = html_to_blocks("<body><p>Keep</p><script>doThing()</script></body>")
        self.assertEqual(texts(result.blocks), ["Keep"])

    def test_page_local_behaviour_is_carried(self):
        # The original rule refused every script, justified by analytics. On a
        # site whose only scripts are its own lightbox and nav, that produces a
        # clone that looks right and does nothing.
        result = html_to_blocks(
            "<body><p>x</p><script>(function(){buildLightbox();})()</script></body>"
        )
        self.assertEqual(len(result.scripts), 1)
        self.assertIn("buildLightbox", result.scripts[0])

    def test_analytics_is_never_carried(self):
        # The narrow, real reason the original rule existed.
        for tracker in (
            "gtag('config','G-X')",
            "fbq('init','1')",
            "(function(h,o,t,j){h.hj=h.hj||function(){}})(window,'hotjar')",
            "window.dataLayer=window.dataLayer||[]",
        ):
            with self.subTest(tracker=tracker[:20]):
                result = html_to_blocks(f"<body><p>x</p><script>{tracker}</script></body>")
                self.assertEqual(result.scripts, [])
                self.assertTrue(any("analytics" in d for d in result.dropped))

    def test_external_scripts_are_never_carried(self):
        result = html_to_blocks(
            '<body><p>x</p><script src="https://cdn.example.test/a.js"></script></body>'
        )
        self.assertEqual(result.scripts, [])
        self.assertTrue(any("external" in d for d in result.dropped))

    def test_script_contents_do_not_leak_in_as_text(self):
        result = html_to_blocks("<body><script>var secret = 1;</script><p>Keep</p></body>")
        self.assertNotIn("var secret = 1;", " ".join(texts(result.blocks)))

    def test_style_blocks_are_skipped(self):
        result = html_to_blocks("<body><style>p{color:red}</style><p>Keep</p></body>")
        self.assertEqual(texts(result.blocks), ["Keep"])

    def test_behavioural_attributes_are_dropped_and_reported(self):
        result = html_to_blocks('<body><div onclick="go()" ng-repeat="x in y">Hi</div></body>')
        self.assertNotIn("onclick", result.blocks[0].get("attributes", {}))
        self.assertEqual(len(result.dropped), 2)

    def test_html_that_converts_to_nothing_is_an_error(self):
        # "Converted to nothing" must not read the same as "nothing to convert".
        with self.assertRaises(ConversionError):
            html_to_blocks("<body><script>only()</script></body>")

    def test_head_is_not_carried_into_the_body(self):
        result = html_to_blocks("<html><head><title>T</title></head><body><p>B</p></body></html>")
        self.assertEqual(texts(result.blocks), ["B"])


class InlineSvgIsOpaqueContent(unittest.TestCase):
    """#15: an <svg>'s attributes ARE its content, not styling metadata —
    the HTML attribute allowlist that correctly filters a <div> would strip
    every one of them and leave a <use> skeleton that paints nothing."""

    SPRITE = (
        '<body><svg style="display:none"><symbol id="icon-x" viewBox="0 0 24 24">'
        '<path d="M0 0L24 24" fill="currentColor"></path>'
        "</symbol></svg>"
        '<svg class="icon"><use href="#icon-x"></use></svg></body>'
    )

    def test_svg_becomes_one_block_not_a_walked_subtree(self):
        result = html_to_blocks(self.SPRITE)
        # Two top-level <svg>s at body level; neither has real children in
        # the block tree — each is one opaque leaf.
        svgs = [b for b in result.blocks if "<svg" in b.get("innerHTML", "")]
        self.assertEqual(len(svgs), 2)
        for block in svgs:
            self.assertNotIn("children", block)

    def test_content_attributes_survive_verbatim(self):
        # Before the fix: viewBox/d/fill all failed KEPT_ATTRIBUTES and were
        # dropped as "not content" — this is the exact 312x/210x from #15.
        result = html_to_blocks(self.SPRITE)
        sprite_block = result.blocks[0]
        self.assertIn('viewbox="0 0 24 24"', sprite_block["innerHTML"])
        self.assertIn('d="M0 0L24 24"', sprite_block["innerHTML"])
        self.assertIn('fill="currentColor"', sprite_block["innerHTML"])
        self.assertIn("<symbol", sprite_block["innerHTML"])
        self.assertIn("<path", sprite_block["innerHTML"])

    def test_the_use_reference_survives_alongside_the_definition(self):
        result = html_to_blocks(self.SPRITE)
        use_block = result.blocks[1]
        self.assertIn('href="#icon-x"', use_block["innerHTML"])

    def test_nothing_from_the_svg_is_reported_as_dropped(self):
        result = html_to_blocks(self.SPRITE)
        self.assertEqual(
            [d for d in result.dropped if "svg" in d or "viewbox" in d or "path" in d], []
        )

    def test_svg_captured_is_counted(self):
        result = html_to_blocks(self.SPRITE)
        self.assertEqual(result.svg_captured, 2)
        self.assertEqual(result.counts["svg_captured"], 2)

    def test_a_self_closing_use_tag_is_handled(self):
        # Real markup mixes both styles; a self-closing <use/> goes through
        # HTMLParser's default handle_startendtag (starttag then endtag).
        result = html_to_blocks('<body><svg><use href="#icon-x"/></svg></body>')
        self.assertIn('href="#icon-x"', result.blocks[0]["innerHTML"])
        self.assertIn("</use>", result.blocks[0]["innerHTML"])

    def test_text_inside_svg_is_escaped_not_dropped(self):
        result = html_to_blocks(
            '<body><svg><text>1 &lt; 2 &amp; 3</text></svg></body>'
        )
        self.assertIn("1 &lt; 2 &amp; 3", result.blocks[0]["innerHTML"])

    def test_an_svg_inside_a_template_stays_inert(self):
        # A sprite sitting inside a JS-only <template> is not live page
        # content — it must stay dropped like everything else in <template>,
        # not get pulled out as if #15's fix applied to it.
        result = html_to_blocks(
            '<body><template><svg><path d="M0 0"></path></svg></template>'
            "<p>Real</p></body>"
        )
        self.assertEqual(texts(result.blocks), ["Real"])
        self.assertEqual(result.svg_captured, 0)

    def test_a_top_level_icon_svg_is_captured_the_same_way_as_a_sprite(self):
        # Not just sprite-sheet definitions — an ordinary standalone icon
        # <svg> used directly gets the same opaque treatment.
        result = html_to_blocks(
            '<body><svg viewBox="0 0 16 16"><circle cx="8" cy="8" r="8"></circle></svg></body>'
        )
        self.assertIn('cx="8"', result.blocks[0]["innerHTML"])
        self.assertNotIn("children", result.blocks[0])

    def test_an_unclosed_non_void_svg_child_does_not_swallow_the_document(self):
        # Review on #15's own PR: a raw depth counter desyncs permanently
        # when a conventionally-empty SVG element (circle/rect/stop/...)
        # is written without its closing tag or a self-closing slash — a
        # common minifier/hand-authoring shape. html.parser never fires a
        # matching handle_endtag for it, so the real </svg> — and every
        # element after it — must not vanish into the buffer.
        result = html_to_blocks(
            '<body><svg><circle cx="1" cy="1" r="1"><path d="M0 0"/></svg>'
            "<p>Real</p><footer>Footer</footer></body>"
        )
        page_text = [t for t in texts(result.blocks) if "<svg" not in t]
        self.assertEqual(page_text, ["Real", "Footer"])
        svg_block = next(b for b in result.blocks if "innerHTML" in b and "<svg" in b["innerHTML"])
        self.assertIn("<circle", svg_block["innerHTML"])
        self.assertIn("</svg>", svg_block["innerHTML"])
        self.assertEqual(result.svg_captured, 1)

    def test_an_svg_never_closed_is_flushed_at_end_of_document_not_lost(self):
        # If the document ends (truncated crawl, malformed source) before
        # </svg> ever appears, the buffered subtree must still surface —
        # as a reported oddity, never as silent data loss.
        result = html_to_blocks('<body><svg><path d="M0 0"/>')
        self.assertEqual(result.svg_captured, 1)
        self.assertTrue(any("never closed" in d for d in result.dropped))
        svg_block = next(b for b in result.blocks if "innerHTML" in b and "<svg" in b["innerHTML"])
        self.assertIn("<path", svg_block["innerHTML"])


class Crawling(unittest.TestCase):
    def _site(self, d):
        root = Path(d)
        (root / "about").mkdir(parents=True)
        (root / "index.html").write_text("<html><head><title>Home</title></head>"
                                         "<body><h1>Home</h1></body></html>")
        (root / "about" / "index.html").write_text("<html><head><title>About</title></head>"
                                                   "<body><h1>About</h1></body></html>")
        (root / "logo.png").write_bytes(b"\x89PNG")
        return root

    def test_local_crawl_finds_pages_and_notes_assets(self):
        with tempfile.TemporaryDirectory() as d:
            crawl = crawl_local(self._site(d))
        self.assertEqual(sorted(crawl.pages), ["", "about"])
        self.assertEqual(crawl.counts["assets_referenced"], 1)

    def test_index_html_and_trailing_slash_are_the_same_route(self):
        self.assertEqual(route_for("/about/index.html", "http://example.test/"), "about")
        self.assertEqual(route_for("/about/", "http://example.test/"), "about")
        self.assertEqual(route_for("/about", "http://example.test/"), "about")

    def test_an_empty_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError) as caught:
                crawl_local(d)
        self.assertIn("empty site", str(caught.exception))


class Replicating(unittest.TestCase):
    def _crawl(self):
        return CrawlResult(
            pages={
                "": "<html><head><title>Acme — Home</title></head>"
                    "<body><h1>Welcome</h1></body></html>",
                "about": "<html><head><title>About</title></head>"
                         "<body><p>Since 1999.</p></body></html>",
            }
        )

    def test_every_route_becomes_a_page(self):
        result = replicate(self._crawl(), site="acme")
        self.assertEqual(result.coverage, 1.0)
        # The crawl calls the site root "", but a page cannot hold an empty
        # route — Builder rewrites it to pages/<hash> and the homepage becomes
        # unreachable (TRAP-014). It becomes "home", and the site's front door
        # is Website Settings.home_page, which `prerequisites` demands.
        self.assertEqual(sorted(p.route for p in result.pages), ["about", "home"])

    def test_the_home_page_requires_the_website_setting(self):
        result = replicate(self._crawl(), site="acme")
        self.assertTrue(any("home_page" in p for p in result.prerequisites))
        self.assertIn("home_page", result.summary())

    def test_titles_come_from_the_source(self):
        result = replicate(self._crawl(), site="acme")
        self.assertIn("Acme — Home", [p.title for p in result.pages])

    def test_a_template_is_always_emitted(self):
        result = replicate(self._crawl(), site="acme")
        self.assertIsNotNone(result.template)
        self.assertTrue(result.template.is_template)

    def test_an_unconvertible_page_is_reported_not_fatal(self):
        # One bad page must not abort a 200-page replication, and must not
        # vanish from the report either.
        crawl = self._crawl()
        crawl.pages["broken"] = "<body><script>only()</script></body>"
        result = replicate(crawl, site="acme")
        self.assertLess(result.coverage, 1.0)
        self.assertIn("INCOMPLETE", result.summary())
        self.assertTrue(any("broken" in w for w in result.warnings))

    def test_a_truncated_crawl_is_surfaced(self):
        crawl = self._crawl()
        crawl.truncated = True
        result = replicate(crawl, site="acme")
        self.assertTrue(any("truncated" in w for w in result.warnings))

    def test_pages_are_unpublished_by_default(self):
        # Publishing a replication before anyone has looked at it is how a
        # half-converted site goes live.
        result = replicate(self._crawl(), site="acme")
        self.assertTrue(all(not p.published for p in result.pages))


if __name__ == "__main__":
    unittest.main()


class ShellCrawlsRefuse(unittest.TestCase):
    """The success-shaped failure (#5): a client-side-rendered site serves a
    bootstrap shell; a static crawl of it must refuse (exit 2), not convert a
    husk at 100%. Thresholds are pinned against the husk that motivated this:
    51 KB of markup, 15 scripts, 22 visible characters."""

    SHELL = ("<html><head><title>Home | A Site</title>"
             + "<script src=\"/a.js\"></script>" * 6
             + "<style>" + ("body{}" * 400) + "</style></head>"
             + "<body><div id=\"root\"></div>"
             + "<script>" + ("var x=1;" * 400) + "</script></body></html>")

    CONTENT = ("<html><body><h1>Hours</h1>"
               + "<p>Open every day, come on in.</p>" * 30
               + "</body></html>")

    @staticmethod
    def _opener_for(pages):
        def fetch(url, timeout):
            from urllib.parse import urlparse
            return "text/html", pages[urlparse(url).path or "/"]
        return fetch

    def test_an_all_shell_crawl_refuses_and_names_the_fix(self):
        from buildsmith.errors import CouldNotCheck
        with self.assertRaises(CouldNotCheck) as cm:
            crawl_site("http://example.test/",
                       opener=self._opener_for({"/": self.SHELL}))
        self.assertIn("--render", str(cm.exception.message))

    def test_a_rendered_all_shell_crawl_still_refuses(self):
        """--render that still yields husks proved nothing; it must not
        suggest --render as the fix for itself."""
        from buildsmith.errors import CouldNotCheck
        with self.assertRaises(CouldNotCheck) as cm:
            crawl_site("http://example.test/",
                       opener=self._opener_for({"/": self.SHELL}), render=True)
        self.assertNotIn("--render", str(cm.exception.message))

    def test_one_real_page_among_shells_passes(self):
        """The tripwire is all-or-nothing on purpose: partial shells are a
        content gap the feature inventory reports, not a refused crawl."""
        shell = self.SHELL.replace("</body>",
                                   "<a href=\"/hours\">hours</a></body>")
        result = crawl_site(
            "http://example.test/",
            opener=self._opener_for({"/": shell, "/hours": self.CONTENT}))
        self.assertEqual(len(result.pages), 2)

    def test_a_small_plain_page_is_not_a_shell(self):
        """A genuinely minimal page (short text, no script pile) must never
        trip this — the heuristic keys on machinery, not brevity."""
        result = crawl_site(
            "http://example.test/",
            opener=self._opener_for(
                {"/": "<html><body><p>Closed for the season.</p></body></html>"}))
        self.assertEqual(len(result.pages), 1)


class HeadContentShipsJinjaSafe(unittest.TestCase):
    """#14 — Builder renders head_html through Frappe's safe_render Jinja,
    which refuses any string containing `.__` (and the three delimiters).
    Real head content is full of both, so carried CSS/JS must ship as file
    references; one inlined `window.__STATE__` 417s every route on the site."""

    HTML = (
        "<html><head>"
        "<style>#lightbox { position: fixed } .__util { color: red }</style>"
        "<script>window.__STATE__ = {};</script>"
        "</head><body><div class='a'>x</div></body></html>"
    )

    def test_with_assets_dir_head_html_is_references_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "crawl"
            root.mkdir()
            (root / "index.html").write_text(self.HTML)
            assets = Path(tmp) / "assets"
            result = replicate(root, site="example", assets_dir=assets)
            head = result.pages[0].head_html
            for marker in ("{{", "{%", "{#", ".__"):
                self.assertNotIn(marker, head, marker)
            self.assertIn("/files/example-head-", head)
            written = sorted(f.name for f in assets.iterdir())
            self.assertTrue(any(n.endswith(".css") for n in written), written)
            self.assertTrue(any(n.endswith(".js") for n in written), written)
            # The bytes the browser gets are the source's, not a rewrite.
            js = next(f for f in assets.iterdir() if f.suffix == ".js")
            self.assertIn("window.__STATE__", js.read_text())

    def test_without_assets_dir_hostile_inline_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "crawl"
            root.mkdir()
            (root / "index.html").write_text(self.HTML)
            result = replicate(root, site="example")
            # The route is rejected and says why, rather than emitting a page
            # that 417s at first view.
            self.assertEqual(len(result.pages), 0)
            self.assertTrue(any("safe_render" in w for w in result.warnings),
                            result.warnings)


class NestedRoutesSurviveSaving(unittest.TestCase):
    """#7 — the first nested route ever crawled crashed the clone: the write
    loop never made parent directories, though crawl_local could always read
    them back. save_crawl must round-trip whatever crawl_site produces."""

    def test_nested_routes_write_and_read_back(self):
        result = CrawlResult()
        result.pages = {"": "<p>home</p>", "menu": "<p>menu</p>",
                        "s/gallery": "<p>pics</p>"}
        with tempfile.TemporaryDirectory() as tmp:
            save_crawl(result, tmp)
            back = crawl_local(tmp)
        self.assertEqual(set(back.pages), set(result.pages))


class RoutesSurviveTheFilesystem(unittest.TestCase):
    """A crawl saved to disk and read back must keep the source's routes.

    Found by dogfooding a real site: pages served at extensionless routes
    like /menu were coming back as /menu.html, so the replication would have
    published every page at a URL the source never had. Routes preserved is W1's
    success floor, and this broke it invisibly.
    """

    def test_a_html_suffix_is_a_filename_not_a_route(self):
        self.assertEqual(route_for("/menu.html", "http://example.test/"), "menu")
        self.assertEqual(route_for("/menu.htm", "http://example.test/"), "menu")

    def test_index_still_collapses_to_the_parent(self):
        self.assertEqual(route_for("/index.html", "http://example.test/"), "")
        self.assertEqual(route_for("/about/index.html", "http://example.test/"), "about")

    def test_extensionless_routes_are_untouched(self):
        self.assertEqual(route_for("/menu", "http://example.test/"), "menu")
        self.assertEqual(route_for("/a/b/c", "http://example.test/"), "a/b/c")

    def test_a_crawl_saved_and_reloaded_keeps_its_routes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "index.html").write_text("<html><body><h1>Home</h1></body></html>")
            (root / "menu.html").write_text("<html><body><h1>Menu</h1></body></html>")
            reloaded = crawl_local(root)
        self.assertEqual(sorted(reloaded.pages), ["", "menu"])


class AppearanceIsRecovered(unittest.TestCase):
    """A clone with the right words in the wrong places is not a clone.

    The converter originally skipped `<style>` entirely and declared that
    "appearance is not reconstructed". Browsing the result made the problem
    obvious: every class name copied across, no rule behind any of them.
    """

    def test_class_rules_become_block_styles(self):
        html = (
            "<html><head><style>.hero { background-color: #0a7d55; min-height: 600px }"
            "</style></head><body><div class='hero'>Hi</div></body></html>"
        )
        result = html_to_blocks(html)
        self.assertEqual(
            result.blocks[0]["baseStyles"],
            {"backgroundColor": "#0a7d55", "minHeight": "600px"},
        )
        self.assertEqual(result.counts["styles_recovered"], 1)

    def test_an_inline_style_beats_a_class_rule(self):
        # Same precedence the browser applies.
        html = (
            "<html><head><style>.a { color: red }</style></head>"
            "<body><div class='a' style='color: blue'>x</div></body></html>"
        )
        self.assertEqual(html_to_blocks(html).blocks[0]["baseStyles"]["color"], "blue")

    def test_mobile_media_queries_land_in_the_mobile_bucket(self):
        html = (
            "<html><head><style>@media only screen and (max-width: 576px)"
            "{ .a { display: none } }</style></head>"
            "<body><div class='a'>x</div></body></html>"
        )
        self.assertEqual(html_to_blocks(html).blocks[0]["mobileStyles"], {"display": "none"})

    def test_rules_that_match_no_block_are_kept_for_head_html(self):
        # An element a script creates at runtime cannot have block styles — the
        # element does not exist until the script runs.
        html = (
            "<html><head><style>#lightbox { position: fixed }</style></head>"
            "<body><div class='a'>x</div></body></html>"
        )
        result = html_to_blocks(html)
        self.assertIn("#lightbox", result.leftover_css)

    def test_a_descendant_selector_is_not_guessed_onto_a_block(self):
        # Attributing `.a .b` to one element would silently restyle the wrong one.
        html = (
            "<html><head><style>.a .b { color: red }</style></head>"
            "<body><div class='a'><span class='b'>x</span></div></body></html>"
        )
        result = html_to_blocks(html)
        self.assertNotIn("baseStyles", result.blocks[0])
        self.assertIn(".a .b", result.leftover_css)


class LinkedStylesheetsAreRecovered(unittest.TestCase):
    """Most real sites keep ~all their CSS in <link rel=stylesheet> files.

    Recovering only <style> elements converted those sites with zero styles —
    "all the right words in all the wrong places" — while the conversion
    account said nothing (bootstrap critical review §2.3, private notes).
    """

    HTML = (
        "<html><head><link rel='stylesheet' href='css/site.css'></head>"
        "<body><div class='hero'>Hi</div></body></html>"
    )
    CSS = ".hero { background-color: #0a7d55 }"

    def test_a_linked_sheet_is_folded_into_style_recovery(self):
        result = html_to_blocks(self.HTML, css_loader=lambda href: self.CSS)
        self.assertEqual(result.blocks[0]["baseStyles"],
                         {"backgroundColor": "#0a7d55"})
        self.assertEqual(result.counts["styles_recovered"], 1)

    def test_attribute_order_does_not_hide_the_link(self):
        # href before rel is equally legal HTML.
        html = self.HTML.replace("rel='stylesheet' href='css/site.css'",
                                 "href='css/site.css' rel='stylesheet'")
        result = html_to_blocks(html, css_loader=lambda href: self.CSS)
        self.assertEqual(result.counts["styles_recovered"], 1)

    def test_an_unresolvable_sheet_is_reported_not_silently_skipped(self):
        result = html_to_blocks(self.HTML, css_loader=lambda href: None)
        self.assertTrue(any("css/site.css" in d and "not folded" in d
                            for d in result.dropped))

    def test_no_loader_is_reported_too(self):
        result = html_to_blocks(self.HTML)
        self.assertTrue(any("no css_loader" in d for d in result.dropped))

    def test_a_non_stylesheet_link_is_still_just_skipped(self):
        html = ("<html><head><link rel='preconnect' href='https://x.example'>"
                "</head><body><p>x</p></body></html>")
        result = html_to_blocks(html, css_loader=lambda href: self.CSS)
        self.assertEqual(result.counts["styles_recovered"], 0)

    def test_an_inline_style_element_beats_a_linked_sheet(self):
        # The framework-sheet-plus-page-override pattern: the browser renders
        # the LATER rule (the inline <style>), so the conversion must too.
        # _apply_stylesheet's merge is earlier-wins, hence the reverse-cascade
        # feed order — this pins it.
        html = (
            "<html><head><link rel='stylesheet' href='a.css'>"
            "<style>.hero { color: blue }</style></head>"
            "<body><div class='hero'>Hi</div></body></html>"
        )
        result = html_to_blocks(html,
                                css_loader=lambda h: ".hero { color: red }")
        self.assertEqual(result.blocks[0]["baseStyles"]["color"], "blue")

    def test_a_percent_encoded_href_still_resolves(self):
        # fetch_assets saves by the DECODED basename; the loader must match.
        from buildsmith.workflows.replicate.build import _css_loader_for

        with tempfile.TemporaryDirectory() as tmp:
            assets = Path(tmp)
            (assets / "my style.css").write_text(self.CSS)
            loader = _css_loader_for(None, assets)
            self.assertEqual(loader("/css/my%20style.css"), self.CSS)

    def test_a_traversal_href_cannot_read_outside_the_crawl(self):
        # The crawl is untrusted input; ../ must not fold arbitrary readable
        # .css files into the emitted page.
        from buildsmith.workflows.replicate.build import _css_loader_for

        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside.css"
            outside.write_text(".leak { color: red }")
            root = Path(tmp) / "crawl"
            root.mkdir()
            loader = _css_loader_for(root, None)
            self.assertIsNone(loader("../outside.css"))

    def test_replicate_resolves_sheets_from_a_local_crawl(self):
        # End to end: the page links a sheet that exists in the crawl
        # directory; the emitted page carries the recovered style.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "css").mkdir()
            (root / "css" / "site.css").write_text(self.CSS)
            (root / "index.html").write_text(self.HTML)
            result = replicate(root, site="acme")
        blocks = result.pages[0].blocks
        hero = blocks[0]
        self.assertEqual(hero.get("baseStyles", {}).get("backgroundColor"),
                         "#0a7d55")


class StylesheetLinksAreCrawled(unittest.TestCase):
    def test_links_collects_stylesheets_in_either_attribute_order(self):
        from buildsmith.workflows.replicate.crawl import _links

        html = ("<html><head>"
                "<link rel='stylesheet' href='/a.css'>"
                "<link href='/b.css' rel='stylesheet'>"
                "<link rel='preconnect' href='https://cdn.example'>"
                "</head><body></body></html>")
        _, assets = _links(html, "http://x.example/")
        self.assertIn("http://x.example/a.css", assets)
        self.assertIn("http://x.example/b.css", assets)
        self.assertNotIn("https://cdn.example", assets)


class BuilderNativeShapeTest(unittest.TestCase):
    """The converter must emit a document Builder itself could have produced.

    Both cases here shipped to a real dev instance and were found by the owner
    looking at the editor, not by this suite. The published page looked correct
    in both cases; only the editor showed the damage. That is the whole reason
    these assert on the *block tree* rather than on rendered output.
    """

    # Frappe Builder's own page template, reduced. Note there is no <body> tag —
    # that is not a simplification, it is what Builder emits.
    BUILDER_DOC = (
        "<!DOCTYPE html>\n<!-- Made with Frappe Builder -->\n"
        '<html lang="en">\n<head>\n<title>Menu &middot; Somewhere</title>\n'
        "<style>.c1 { font-family: Skybald, Merriline, cursive; color: #111 }</style>\n"
        "</head>\n"
        '<div class="c1"><div class="c2">Hello</div></div>\n</html>'
    )

    def _all(self, blocks):
        out = []

        def walk(b):
            out.append(b)
            for c in b.get("children") or []:
                walk(c)

        for b in blocks:
            walk(b)
        return out

    def test_document_skeleton_never_becomes_blocks(self) -> None:
        """`<html>`/`<head>`/`<title>` are not content.

        The old code looked for `<body>` and silently did nothing when there was
        none — so Builder's own output, which has no `<body>`, kept `<html>` as
        the root block. An unstyled `<html>` root shrink-wraps in the editor
        canvas: the page renders as a narrow left-aligned column.
        """
        result = html_to_blocks(self.BUILDER_DOC)
        elements = {b.get("element") for b in self._all(result.blocks)}
        for skeleton in ("html", "head", "body", "title"):
            self.assertNotIn(skeleton, elements)

    def test_root_is_an_ordinary_container(self) -> None:
        """Builder's own pages root on a div; blockTemplate.ts emits nothing else."""
        result = html_to_blocks(self.BUILDER_DOC)
        self.assertEqual([b.get("element") for b in result.blocks], ["div"])

    def test_title_is_carried_out_of_the_tree(self) -> None:
        """It belongs in page_title, but it must not be lost either."""
        self.assertEqual(html_to_blocks(self.BUILDER_DOC).title, "Menu · Somewhere")

    def test_a_real_body_is_still_preferred(self) -> None:
        """Handwritten HTML that does have a <body> must keep working."""
        result = html_to_blocks(
            "<html><head><title>T</title></head><body><div class='x'>hi</div></body></html>"
        )
        self.assertEqual([b.get("element") for b in result.blocks], ["div"])
        self.assertEqual(result.title, "T")

    def test_unwrapping_is_reported_not_silent(self) -> None:
        """A structural rewrite the caller did not ask for must be visible."""
        result = html_to_blocks(self.BUILDER_DOC)
        self.assertTrue(
            any("no <body>" in d for d in result.dropped),
            f"unwrapping was not reported: {result.dropped}",
        )

    def test_font_family_is_a_single_family(self) -> None:
        """Builder's editor cannot parse a stack.

        `fontManager.ts` does `encodeURIComponent(font)` on the whole value, so a
        stack produces `family=Skybald%2C%20Merriline%2C%20cursive`, which Google
        Fonts rejects. The font silently fails to load in the editor only — the
        published page is fine, so nothing downstream notices.
        """
        result = html_to_blocks(self.BUILDER_DOC)
        families = {
            (b.get(bucket) or {}).get("fontFamily")
            for b in self._all(result.blocks)
            for bucket in ("baseStyles", "rawStyles", "mobileStyles", "tabletStyles")
        } - {None}
        self.assertEqual(families, {"Skybald"})
        for family in families:
            self.assertNotIn(",", family)

    def test_inline_style_font_family_is_also_reduced(self) -> None:
        """An inline style wins the specificity merge, so it must be fixed too —
        and it is what carries a site's most deliberate typography."""
        result = html_to_blocks(
            "<html><div style=\"font-family: 'Open Sans', ui-sans-serif, sans-serif\">"
            "hi</div></html>"
        )
        block = self._all(result.blocks)[0]
        self.assertEqual(block["baseStyles"]["fontFamily"], "Open Sans")

    def test_escaped_spaces_in_a_family_name_are_undone(self) -> None:
        """Builder's generated CSS escapes spaces inside family names too.

        Splitting before undoing that yields a family literally called
        `Open\\ Sans`, which Builder sends to Google Fonts as `Open%5C%20Sans` —
        the same 400 this reduction exists to prevent, just for two-word
        families rather than stacks. Found reviewing the fix, not by the fix.
        """
        result = html_to_blocks(
            "<html><head><style>"
            ".c1 { font-family: Open\\ Sans, ui-sans-serif, sans-serif }"
            "</style></head><div class='c1'>hi</div></html>"
        )
        block = self._all(result.blocks)[0]
        self.assertEqual(block["baseStyles"]["fontFamily"], "Open Sans")

    def test_title_entities_are_decoded(self) -> None:
        """`page_title` used to come from a regex over the raw HTML, which
        decoded nothing — an entity reached the field still escaped."""
        result = html_to_blocks(
            "<html><head><title>Menu &amp; More &middot; Somewhere</title></head>"
            "<div>hi</div></html>"
        )
        self.assertEqual(result.title, "Menu & More · Somewhere")
