"""Convert HTML into Builder blocks, faithfully.

Faithful is the whole requirement. W1's success floor is *all original content
present*, so this converter is judged on what it fails to carry across, and it
reports that rather than leaving you to notice.

Three rules shape the implementation:

**Content is never silently dropped.** Anything not converted — a `<script>`, an
unknown element, a comment — is counted and reported in `ConversionResult.
dropped`. A converter that quietly loses a phone number is worse than one that
refuses, because the loss surfaces months later as "the site is missing a page".

**Scripts and styles are not content.** `<script>` never becomes a block: it is
behaviour, it will not work when re-hosted, and importing it into a page is how a
replication carries someone else's tracking pixel into a new site. `<style>` is
likewise skipped — the design system replaces it.

**Appearance is recovered as block styles, not as a stylesheet.** Inline `style`
attributes convert to `baseStyles`. So do the rules in the page's own `<style>`
blocks: each `.cls { … }` is matched against the elements carrying that class and
folded into their style buckets, with `max-width` media queries going to
`mobileStyles`/`tabletStyles` at Builder's own breakpoints.

That is the native form. Builder regenerates its CSS from block styles, so a
replica whose blocks carry the styles renders like the original *and* stays
editable; injecting the source stylesheet wholesale would render the same and
leave every value invisible to the design system.

An earlier version of this converter skipped `<style>` entirely and declared that
"appearance is not reconstructed". The result was a replica with all the right
words in all the wrong places — which is not a faithful copy of anything, and is
not what W1 promises. Found by looking at one.

Zero dependencies — stdlib `html.parser`, so this runs anywhere.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from buildsmith.primitives.blocks import BlockError

__all__ = [
    "ConversionError",
    "ConversionResult",
    "SKIPPED_ELEMENTS",
    "VOID_ELEMENTS",
    "html_to_blocks",
]


class ConversionError(BlockError):
    """The HTML could not be converted."""


#: Third-party tracking, by any name. Never carried into a clone: importing
#: somebody's analytics into a new site is a privacy problem and a data-quality
#: one, and it was the original reason this converter refused all scripts.
ANALYTICS = (
    "googletagmanager", "google-analytics", "gtag(", "gtm.js", "fbq(", "fbevents",
    "hotjar", "segment.com", "analytics.js", "mixpanel", "clarity.ms", "matomo",
    "piwik", "plausible", "posthog", "amplitude", "_paq", "dataLayer",
)

#: Builder emits its own runtime shims; re-importing them would duplicate what
#: the target Builder already provides.
BUILDER_BOILERPLATE = ("window.process", "window.events", "frappe.ready")

#: Never becomes a block. `<style>` is still not a block — but it is now *read*
#: before being discarded, so its rules can become block styles.
SKIPPED_ELEMENTS = frozenset({"script", "style", "noscript", "template", "link", "meta"})

#: Builder's own breakpoints, so recovered media queries land in the bucket
#: Builder would have used. From the pinned renderer's MOBILE_BREAKPOINT /
#: DESKTOP_BREAKPOINT.
MOBILE_MAX = 576
TABLET_MAX = 1024

#: Elements with no closing tag. Tracked so the tree does not go inside them.
VOID_ELEMENTS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input",
     "link", "meta", "param", "source", "track", "wbr"}
)

#: Attributes worth carrying. An allowlist rather than a blocklist: framework
#: attributes (`data-reactroot`, `ng-*`, `x-on:*`) describe behaviour that will
#: not exist in Builder, and copying them produces markup that looks meaningful
#: and is inert.
KEPT_ATTRIBUTES = frozenset(
    {"href", "src", "alt", "title", "target", "rel", "type", "value",
     "colspan", "rowspan", "width", "height", "loading"}
)

_STYLE_PROPERTY = re.compile(r"^\s*([a-zA-Z-]+)\s*:\s*(.+?)\s*$")

#: Internal marker on a block that only exists to carry a run of text. Stripped
#: by `_collapse_text()` before anything leaves this module.
_TEXT_RUN = "__text_run__"


def _camel(prop: str) -> str:
    """`background-color` → `backgroundColor`, which is how Builder stores it."""
    head, *rest = prop.strip().lower().split("-")
    return head + "".join(part.capitalize() for part in rest if part)


def _parse_inline_style(value: str) -> dict[str, str]:
    styles: dict[str, str] = {}
    for declaration in value.split(";"):
        if not declaration.strip():
            continue
        match = _STYLE_PROPERTY.match(declaration)
        if match:
            prop, value = _camel(match.group(1)), match.group(2)
            # Same reshaping as class rules get. An inline style attribute wins
            # the specificity merge, so leaving it unfixed here would put a font
            # stack back on the block and undo the fix for exactly the elements
            # that carry the site's most deliberate typography.
            fix = _VALUE_FIXUPS.get(prop)
            styles[prop] = fix(value) if fix else value
    return styles


@dataclass
class ConversionResult:
    """Blocks, plus an honest account of what did not come across."""

    blocks: list[dict] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    text_length: int = 0
    element_count: int = 0
    #: Style rules folded out of the page's own <style> blocks onto blocks.
    styles_recovered: int = 0
    #: CSS that could not become a block style — id selectors, descendant
    #: selectors, and rules for elements JavaScript creates at runtime. It is
    #: still the page's appearance, so it is kept for `head_html` rather than
    #: dropped.
    leftover_css: str = ""
    #: The document's <title>, if it had one. Belongs in `Builder Page.page_title`,
    #: not in the block tree — Builder owns the document head.
    title: str = ""
    #: Page-local scripts worth carrying. External and analytics-shaped scripts
    #: are excluded — see `_worth_carrying`.
    scripts: list[str] = field(default_factory=list)
    #: <svg> subtrees carried verbatim as opaque innerHTML blocks (#15) —
    #: sprite-sheet definitions and inline icons alike.
    svg_captured: int = 0

    @property
    def counts(self) -> dict[str, int]:
        return {
            "blocks": len(self.blocks),
            "elements": self.element_count,
            "dropped": len(self.dropped),
            "text_characters": self.text_length,
            "styles_recovered": self.styles_recovered,
            "leftover_css_chars": len(self.leftover_css),
            "scripts_carried": len(self.scripts),
            "svg_captured": self.svg_captured,
        }


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root: dict[str, Any] = {"element": "__root__", "children": []}
        self.stack: list[dict] = [self.root]
        self.dropped: list[str] = []
        self.text_length = 0
        self.element_count = 0
        self._skip_depth = 0
        self._skipping: str | None = None
        self._capturing_style = False
        self._capturing_script = False
        #: An <svg> subtree is foreign-namespace content: its attributes
        #: (`d`, `viewBox`, `points`, `cx`/`cy`/`r`, …) ARE the drawing, not
        #: styling metadata, so KEPT_ATTRIBUTES — built for HTML — would
        #: strip every one of them and leave a paint-nothing <use> skeleton
        #: (#15). Rebuilt verbatim from parser events into one opaque block
        #: instead of walked like ordinary HTML.
        self._capturing_svg = False
        self._svg_depth = 0
        self._svg_buffer: list[str] = []
        self.svg_count = 0
        self.stylesheet: list[str] = []
        #: hrefs of <link rel="stylesheet">, in document order. The sheets
        #: themselves live in the crawl; html_to_blocks resolves them through
        #: its css_loader and folds them into style recovery.
        self.linked_stylesheets: list[str] = []
        self.scripts: list[str] = []

    @staticmethod
    def _render_starttag(tag: str, attrs: list[tuple[str, str | None]]) -> str:
        """Reconstruct `<tag attr="val" …>` from parsed events.

        Rebuilding from what the parser already extracted, rather than
        slicing raw source, sidesteps `HTMLParser`'s offset bookkeeping —
        and it's the same "build from callbacks" style everything else in
        this class already uses.
        """
        parts = [tag]
        for name, value in attrs:
            parts.append(name if value is None else f'{name}="{html.escape(value, quote=True)}"')
        return f"<{' '.join(parts)}>"

    # -- elements ---------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skipping:
            # An <svg> inside <template>/<noscript> is still inert content —
            # this check has to run BEFORE svg-capture starts, or an icon
            # sprite sitting inside a JS-only template would get pulled out
            # as if it were live page content.
            if tag == self._skipping:
                self._skip_depth += 1
            return

        if self._capturing_svg:
            self._svg_buffer.append(self._render_starttag(tag, attrs))
            self._svg_depth += 1
            return

        if tag == "svg":
            self._capturing_svg = True
            self._svg_depth = 1
            self._svg_buffer = [self._render_starttag(tag, attrs)]
            return

        if tag in SKIPPED_ELEMENTS:
            if tag not in VOID_ELEMENTS:
                self._skipping = tag
                self._skip_depth = 1
            if tag == "style":
                # Read, then discard. The element never becomes a block, but its
                # rules are the page's appearance and we want them.
                self._capturing_style = True
            elif tag == "script":
                # An external script is somebody else's code on somebody else's
                # terms; only page-local behaviour is a candidate.
                external = any(name == "src" for name, _ in attrs)
                if external:
                    self.dropped.append("<script src=…> not carried: external code")
                else:
                    self._capturing_script = True
            elif tag == "link":
                # A stylesheet link is the page's appearance by reference —
                # most real sites keep ~all their CSS here, not in <style>.
                # Record the href; whether the sheet is available is decided
                # in html_to_blocks, which owns the reporting.
                attributes = {n: v for n, v in attrs if v is not None}
                rel = attributes.get("rel", "").lower().split()
                if "stylesheet" in rel and attributes.get("href"):
                    self.linked_stylesheets.append(attributes["href"])
                else:
                    self.dropped.append(
                        "<link> skipped: behaviour or presentation, not content"
                    )
            else:
                self.dropped.append(
                    f"<{tag}> skipped: behaviour or presentation, not content"
                )
            return

        block: dict[str, Any] = {"element": tag}
        attributes: dict[str, str] = {}
        classes: list[str] = []

        for name, value in attrs:
            if value is None:
                continue
            if name == "class":
                classes = [c for c in value.split() if c]
            elif name == "style":
                parsed = _parse_inline_style(value)
                if parsed:
                    block["baseStyles"] = parsed
            elif name in KEPT_ATTRIBUTES:
                attributes[name] = value
            elif name.startswith("data-") or name.startswith("aria-") or name == "id":
                attributes[name] = value
            else:
                self.dropped.append(f"<{tag} {name}=…> attribute dropped: not content")

        if attributes:
            block["attributes"] = attributes
        if classes:
            block["classes"] = classes

        self.element_count += 1
        self.stack[-1].setdefault("children", []).append(block)
        if tag not in VOID_ELEMENTS:
            self.stack.append(block)

    def handle_endtag(self, tag: str) -> None:
        if self._skipping:
            if tag == self._skipping:
                self._skip_depth -= 1
                if self._skip_depth <= 0:
                    self._skipping = None
                    self._capturing_style = False
                    self._capturing_script = False
            return
        if self._capturing_svg:
            self._svg_buffer.append(f"</{tag}>")
            self._svg_depth -= 1
            if self._svg_depth <= 0:
                self._capturing_svg = False
                markup = "".join(self._svg_buffer)
                self._svg_buffer = []
                self.svg_count += 1
                self.element_count += 1
                # A div wrapper, not element="svg": innerHTML is set through
                # a real DOM API, which parses embedded foreign-namespace
                # markup correctly regardless of the parent tag — no need
                # for Builder's block schema to know "svg" as an element
                # type. Not pushed onto self.stack: it's a leaf, opaque.
                self.stack[-1].setdefault("children", []).append(
                    {"element": "div", "innerHTML": markup}
                )
            return
        if tag in VOID_ELEMENTS or tag in SKIPPED_ELEMENTS:
            return
        # Unbalanced markup is the norm in the wild. Unwind to the nearest
        # matching open element rather than trusting the document, and never
        # pop past the root.
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].get("element") == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if self._capturing_style:
            self.stylesheet.append(data)
            return
        if self._capturing_script:
            self.scripts.append(data)
            return
        if self._skipping:
            return
        if self._capturing_svg:
            self._svg_buffer.append(html.escape(data))
            return
        text = data.strip()
        if not text:
            return
        self.text_length += len(text)

        # Builder has no text node, so text has to live on an element. Setting
        # the parent's innerHTML is right for a text-only element and WRONG for
        # mixed content: Builder emits innerHTML first and then the children, so
        # "Some <b>bold</b> text." would render as "Some text.bold" — every word
        # present, in the wrong order, which is the kind of corruption nobody
        # notices until a customer does.
        #
        # So every text run becomes a marked child here, in document order, and
        # _collapse_text() folds the simple case back into innerHTML afterwards.
        self.stack[-1].setdefault("children", []).append(
            {"element": "span", "innerHTML": text, _TEXT_RUN: True}
        )

    def handle_comment(self, data: str) -> None:
        if self._capturing_svg:
            self._svg_buffer.append(f"<!--{data}-->")
            return
        if data.strip():
            self.dropped.append("HTML comment dropped")


_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_MEDIA = re.compile(r"@media([^{]*)\{(.*)", re.S)


def _parse_stylesheet(css: str) -> dict[str, list[tuple[str, dict[str, str]]]]:
    """Class name -> [(bucket, declarations)].

    Only single-class selectors are recovered. A descendant or compound selector
    (`.a .b`, `.a:hover`) cannot be attributed to one block, and guessing which
    element it belongs to would silently restyle the wrong thing — so those are
    reported as dropped rather than approximated.
    """
    styles: dict[str, list[tuple[str, dict[str, str]]]] = {}

    def add(selector: str, body: str, bucket: str) -> None:
        selector = selector.strip()
        if not re.fullmatch(r"\.[\w-]+", selector):
            return
        declarations = {}
        for part in body.split(";"):
            if ":" not in part:
                continue
            prop, _, value = part.partition(":")
            if prop.strip() and value.strip():
                declarations[_camel(prop)] = value.strip().replace("\\ ", " ")
        if declarations:
            styles.setdefault(selector[1:], []).append((bucket, declarations))

    # Media blocks first, so their inner rules are attributed to the right bucket.
    depth, index, plain = 0, 0, []
    while index < len(css):
        at = css.find("@media", index)
        if at == -1:
            plain.append(css[index:])
            break
        plain.append(css[index:at])
        brace = css.find("{", at)
        condition = css[at + 6 : brace]
        depth, cursor = 1, brace + 1
        while cursor < len(css) and depth:
            depth += (css[cursor] == "{") - (css[cursor] == "}")
            cursor += 1
        inner = css[brace + 1 : cursor - 1]
        width = re.search(r"max-width:\s*(\d+)", condition)
        if width:
            limit = int(width.group(1))
            bucket = "mobileStyles" if limit <= MOBILE_MAX else (
                "tabletStyles" if limit <= TABLET_MAX else "baseStyles"
            )
            for selector, body in _RULE.findall(inner):
                add(selector, body, bucket)
        index = cursor

    for selector, body in _RULE.findall("".join(plain)):
        add(selector, body, "baseStyles")
    return styles


def _primary_font_family(value: str) -> str:
    """The first family in a CSS font stack, as Builder's own picker would store it.

    Builder treats `font-family` as ONE family name, not a stack. Its renderer
    happens to cope (`get_google_font_urls` splits and takes the first), but its
    editor does not: `fontManager.ts` does `encodeURIComponent(font)` on the whole
    value, so a stack becomes

        fonts.googleapis.com/css2?family=Skybald%2C%20Merriline%2C%20cursive

    which Google Fonts rejects. The font then silently never loads *in the editor
    only* — the published page looks right, the WYSIWYG does not.

    Builder's font picker only ever writes a single family, so a stack is data
    Builder cannot produce and does not accept. We store what it expects.

    The explicit fallback chain is lost. That is deliberate and it is the same
    thing an author using Builder's own picker gets: the browser falls back to its
    default if the webface fails, rather than to the source's chosen next font.
    """
    # Builder's generated CSS escapes spaces as `\ `, inside family names as
    # well as between them. Splitting before undoing that turns `Open\ Sans`
    # into a family literally called `Open\ Sans`, which Builder then sends to
    # Google Fonts as `Open%5C%20Sans` — the same 400 this function exists to
    # prevent, just for two-word families instead of stacks.
    first = value.replace("\\ ", " ").split(",")[0].strip()
    return first.strip("'\"")


#: Style properties whose CSS value must be reshaped before Builder will accept
#: it. Each entry is (property-name, transform).
_VALUE_FIXUPS = {
    "font-family": _primary_font_family,
    "fontFamily": _primary_font_family,
}


def _apply_stylesheet(blocks: list[dict], styles: dict, dropped: list[str]) -> int:
    """Fold recovered rules into each block's style buckets. Returns how many."""
    applied = 0

    def walk(block: dict) -> None:
        nonlocal applied
        for cls in block.get("classes") or []:
            for bucket, declarations in styles.get(cls, []):
                # An inline style attribute is more specific than a class rule,
                # so it wins — same order the browser applies them in.
                merged = {**declarations, **(block.get(bucket) or {})}
                for prop, fix in _VALUE_FIXUPS.items():
                    if prop in merged:
                        merged[prop] = fix(merged[prop])
                block[bucket] = merged
                applied += 1
        for child in block.get("children") or []:
            walk(child)

    for block in blocks:
        walk(block)
    return applied


def _worth_carrying(script: str) -> tuple[bool, str]:
    """Whether a page-local script belongs in the clone.

    The original rule here was "never copy a script", justified by analytics.
    That justification is real but it is narrower than the rule: on a site whose
    only scripts are its own lightbox and nav behaviour, refusing all of them
    produces a clone that looks right and does nothing. So the *analytics* part
    is kept as an explicit exclusion and the rest is carried.
    """
    body = script.strip()
    if not body:
        return False, "empty"
    lowered = body.lower()
    for marker in ANALYTICS:
        if marker.lower() in lowered:
            return False, f"looks like analytics ({marker})"
    if any(marker in body for marker in BUILDER_BOILERPLATE) and len(body) < 600:
        return False, "Builder's own runtime shim; the target provides its own"
    return True, ""


def html_to_blocks(html: str, *, keep_body_only: bool = True,
                   css_loader=None) -> ConversionResult:
    """Convert an HTML document or fragment into Builder blocks.

    `keep_body_only` returns the contents of `<body>` when there is one, which is
    what a page's blocks should hold — `<head>` is metadata, and Builder manages
    its own.
    """
    if not isinstance(html, str):
        raise ConversionError(f"expected HTML text, got {type(html).__name__}")

    parser = _Builder()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # noqa: BLE001 - stdlib parser raises broadly
        raise ConversionError(f"could not parse the HTML: {exc}") from exc

    blocks = _collapse_text(parser.root.get("children") or [])
    for block in blocks:
        block.pop(_TEXT_RUN, None)

    title = ""
    if keep_body_only:
        blocks, title = _unwrap_document(blocks, parser.dropped)

    # Recover appearance from the page's stylesheets — LINKED ones included.
    # Most real sites keep essentially all their CSS in <link rel=stylesheet>
    # files; recovering only <style> elements converted those pages with no
    # appearance at all ("all the right words in all the wrong places").
    # `css_loader(href) -> str | None` resolves a link against the crawl;
    # a sheet that cannot be resolved is REPORTED, never silently skipped.
    #
    # Ordering: _apply_stylesheet's merge is EARLIER-WINS, so rules must be
    # fed in reverse cascade order — inline <style> first (it usually comes
    # after the links and overrides them; the framework-sheet-plus-page-
    # override pattern is the normal case), then linked sheets latest-first.
    external_css: list[str] = []
    for href in parser.linked_stylesheets:
        css = css_loader(href) if css_loader is not None else None
        if css:
            external_css.append(css)
        else:
            parser.dropped.append(
                f"<link rel=stylesheet href={href}> not folded into style "
                "recovery: " + ("stylesheet not found in the crawl"
                                if css_loader is not None
                                else "no css_loader supplied")
            )
    raw_css = "\n".join(parser.stylesheet + list(reversed(external_css)))
    stylesheet = _parse_stylesheet(raw_css)
    applied = _apply_stylesheet(blocks, stylesheet, parser.dropped)

    # Whatever could not become a block style is still the page's appearance.
    # Rules for elements JavaScript creates at runtime cannot possibly be block
    # styles — the element does not exist until the script runs — so they go to
    # head_html rather than being lost.
    present = _all_classes(blocks)
    leftover = []
    for selector, body in _RULE.findall(raw_css):
        sel = selector.strip()
        if not sel or sel.startswith("@"):
            continue
        if re.fullmatch(r"\.[\w-]+", sel) and sel[1:] in present:
            continue          # already folded onto its block
        leftover.append(f"{sel} {{{body.strip()}}}")

    carried, refused = [], []
    for script in parser.scripts:
        ok, why = _worth_carrying(script)
        (carried if ok else refused).append(script if ok else why)
    for why in dict.fromkeys(refused):
        parser.dropped.append(f"<script> not carried: {why}")

    result = ConversionResult(
        blocks=blocks,
        dropped=parser.dropped,
        text_length=parser.text_length,
        element_count=parser.element_count,
        styles_recovered=applied,
        leftover_css="\n".join(leftover),
        title=title,
        scripts=carried,
        svg_captured=parser.svg_count,
    )

    if not blocks and html.strip():
        raise ConversionError(
            "the HTML produced no blocks. Either it is all script/style, or it is not "
            "HTML at all — a page that converts to nothing must not pass as converted."
        )
    return result


def _collapse_text(blocks: list[dict]) -> list[dict]:
    """Fold a lone text run back into its parent's innerHTML, and drop markers.

    Mixed content keeps its wrapper spans, because their order is the meaning.
    A text-only element does not need one, and carrying it would make every
    heading in a replicated site a span inside an h1.
    """
    for block in blocks:
        children = block.get("children") or []
        if not children:
            continue

        if len(children) == 1 and children[0].get(_TEXT_RUN) and not children[0].get("children"):
            block["innerHTML"] = children[0]["innerHTML"]
            del block["children"]
            continue

        for child in children:
            child.pop(_TEXT_RUN, None)
        _collapse_text(children)
    return blocks


def _all_classes(blocks: list[dict]) -> set[str]:
    found: set[str] = set()

    def walk(block: dict) -> None:
        found.update(block.get("classes") or [])
        for child in block.get("children") or []:
            walk(child)

    for block in blocks:
        walk(block)
    return found


#: Document-skeleton elements. These are not content and must never become
#: blocks: Builder owns the document head, and a Builder page's block tree roots
#: on an ordinary container element.
_SKELETON = ("html", "head", "body")


def _unwrap_document(blocks: list[dict], dropped: list[str]) -> tuple[list[dict], str]:
    """Return the page's content blocks, and its <title>.

    This used to be `if there is a <body>, take its children`, which is correct
    for handwritten HTML and silently wrong for the case that matters most:
    **Frappe Builder's own page template emits no `<body>` tag at all.** It emits
    `<html>`, `<head>`, the content, then `</html>`. `html.parser` reports only
    tags that literally appear and does not synthesise the implied body, so the
    lookup returned None, the guard did nothing, and `<html>` and `<head>` became
    the first two blocks of the page.

    The damage was invisible in the published page — a browser tolerates a nested
    `<html>` — and fatal in the editor, where an unstyled `<html>` root shrinks to
    fit its content and the page renders as a narrow left-aligned column.

    So: prefer `<body>`, fall back to unwrapping `<html>`, and drop the head
    subtree whose contents (`<style>`, `<script>`, `<title>`) are collected
    separately. Anything unexpected is reported rather than passed through.
    """
    title = ""
    head = _find(blocks, "head")
    if head is not None:
        node = _find([head], "title")
        if node is not None:
            title = (node.get("innerHTML") or "").strip()

    body = _find(blocks, "body")
    if body is not None:
        return (body.get("children") or []), title

    root = _find(blocks, "html")
    if root is not None:
        content = [
            child for child in (root.get("children") or [])
            if child.get("element") not in _SKELETON
        ]
        dropped.append(
            "no <body> in the source (Frappe Builder emits none); unwrapped <html> "
            f"and dropped <head> — kept {len(content)} content block(s)"
        )
        return content, title

    # No skeleton at all: a fragment. Legitimate, but a skeleton element left
    # loose in the tree is not, so say so rather than shipping it as a block.
    stray = [b.get("element") for b in blocks if b.get("element") in _SKELETON]
    if stray:
        dropped.append(f"skeleton element(s) {stray} at top level with no <html> wrapper")
        blocks = [b for b in blocks if b.get("element") not in _SKELETON]
    return blocks, title


def _find(blocks: list[dict], element: str) -> dict | None:
    for block in blocks:
        if block.get("element") == element:
            return block
        found = _find(block.get("children") or [], element)
        if found is not None:
            return found
    return None
