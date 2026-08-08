"""W1 — turn a crawl into Builder payloads, with every route preserved.

The pipeline: crawl → convert each page → emit pages and the mandatory template.

The success floor is *all original content present, site navigable, routes
preserved*. So this reports coverage rather than asserting it: how many source
routes became pages, how much text came across, and what did not. A replication
that silently drops a page is the failure mode worth engineering against,
because it looks exactly like one that worked.

**It still emits a template.** No exceptions, same as W2 — a replicated site
that cannot be maintained afterwards is a delivery, not a service.

Nothing here touches a site.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from buildsmith.primitives.blocks import BlockError
from buildsmith.primitives.template import (
    HOME_ROUTE,
    Page,
    assert_template_emitted,
    check_routes,
    page,
    page_template,
    prerequisites,
)
from buildsmith.workflows.replicate.crawl import CrawlResult, _link_tags, crawl_local
from buildsmith.workflows.replicate.htmlblocks import ConversionError, html_to_blocks

__all__ = ["ReplicateResult", "replicate"]


def _css_loader_for(root: Path | None, assets_dir: Path | None):
    """Resolve a page's stylesheet href to crawled CSS text, best-effort.

    Tries, in order: the href's path under the crawl root (local crawls keep
    their layout), then its basename in `assets_dir` (fetch_assets saves by
    basename), then its basename under the root. Returns None when nothing
    matches — html_to_blocks reports that as a dropped stylesheet, so a miss
    is visible in the conversion account rather than silent.

    Results are cached per path: a 200-page site referencing one shared
    sheet must read it once, not 200 times.
    """
    cache: dict[Path, str] = {}

    def load(href: str) -> str | None:
        # unquote to match fetch_assets, which saves by the DECODED basename —
        # an encoded "my%20style.css" would otherwise be fetched, then missed.
        clean = urllib.parse.unquote(
            urllib.parse.urlparse(href).path).lstrip("/")
        name = Path(clean).name
        candidates: list[tuple[Path, Path]] = []
        if root is not None and clean:
            candidates.append((root, root / clean))
        if assets_dir is not None and name:
            candidates.append((assets_dir, assets_dir / name))
        if root is not None and name:
            candidates.append((root, root / name))
        for base, path in candidates:
            if path.suffix.lower() != ".css" or not path.is_file():
                continue
            # The crawl is untrusted input: an href of ../../x.css must not
            # read (and fold into the emitted page) files outside the crawl.
            if not path.resolve().is_relative_to(base.resolve()):
                continue
            if path not in cache:
                cache[path] = path.read_text(encoding="utf-8",
                                             errors="replace")
            return cache[path]
        return None

    return load


@dataclass
class ReplicateResult:
    site: str
    template: Page | None = None
    pages: list[Page] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    source_routes: int = 0
    text_characters: int = 0
    #: Things that must exist on the target before these payloads can be applied.
    prerequisites: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "source_routes": self.source_routes,
            "pages": len(self.pages),
            "templates": 1 if self.template else 0,
            "dropped_fragments": len(self.dropped),
            "text_characters": self.text_characters,
        }

    @property
    def coverage(self) -> float:
        """Fraction of source routes that became pages. 1.0 or it is not a copy."""
        return len(self.pages) / self.source_routes if self.source_routes else 0.0

    def summary(self) -> str:
        lines = [
            f"replicate '{self.site}': {len(self.pages)}/{self.source_routes} route(s) "
            f"converted ({self.coverage:.0%}), {self.text_characters} characters of text"
        ]
        if self.coverage < 1.0:
            lines.append(
                "  INCOMPLETE — some source routes produced no page. W1's floor is every "
                "route preserved; anything less is a partial copy that will read as a "
                "finished one."
            )
        for warning in self.warnings:
            lines.append(f"  {warning}")
        if self.prerequisites:
            lines.append("  before applying, the target site needs:")
            lines += [f"    - {p}" for p in self.prerequisites]
        if self.dropped:
            lines.append(f"  {len(self.dropped)} fragment(s) not carried across:")
            seen: dict[str, int] = {}
            for note in self.dropped:
                seen[note] = seen.get(note, 0) + 1
            for note, count in sorted(seen.items(), key=lambda kv: -kv[1])[:8]:
                lines.append(f"    {count:4}x {note}")
        return "\n".join(lines)


def replicate(
    source: str | Path | CrawlResult,
    *,
    site: str,
    template_group: str | None = None,
    project_folder: str | None = None,
    publish: bool = False,
    assets_dir: str | Path | None = None,
) -> ReplicateResult:
    """Convert a crawl into page payloads plus the mandatory template.

    `assets_dir` is where `fetch_assets` saved a remote crawl's files —
    linked stylesheets are resolved there (and under a local crawl's own
    directory) so style recovery sees the CSS most real sites actually use.
    """
    crawl = source if isinstance(source, CrawlResult) else crawl_local(source)
    crawl_root = None if isinstance(source, CrawlResult) else Path(source)
    css_loader = _css_loader_for(
        crawl_root, Path(assets_dir) if assets_dir else None)

    result = ReplicateResult(site=site, source_routes=len(crawl.pages))
    result.warnings.extend(crawl.skipped[:10])
    if crawl.truncated:
        result.warnings.append(
            "the crawl was truncated, so routes are missing before conversion even starts"
        )

    home_blocks: list[dict] | None = None
    favicon: str | None = None

    for source_route in sorted(crawl.pages):
        html = crawl.pages[source_route]
        # The crawl calls the site root "", but a Builder page cannot hold an
        # empty route — Builder rewrites it to pages/<hash> and the homepage
        # becomes unreachable (TRAP-014). Give it a real route; the site's front
        # door is Website Settings.home_page, listed in `prerequisites`.
        route = source_route or HOME_ROUTE
        try:
            converted = html_to_blocks(html, css_loader=css_loader)
        except ConversionError as exc:
            # Report and continue: one unconvertible page must not abort a
            # 200-page replication, but it must not vanish either.
            result.warnings.append(f"{route or '/'}: not converted — {exc}")
            continue

        result.dropped.extend(converted.dropped)
        result.text_characters += converted.text_length
        if home_blocks is None:
            home_blocks = converted.blocks
        if favicon is None:
            favicon = _favicon_for(html)

        try:
            result.pages.append(
                page(
                    title=_title_for(route, converted.title),
                    route=route,
                    blocks=converted.blocks,
                    template=_placeholder_template(site),
                    published=publish,
                    project_folder=project_folder,
                    favicon=favicon,
                    head_html=_head_html_for(
                        converted, site=site, route=route, html=html,
                        assets_dir=Path(assets_dir) if assets_dir else None),
                    scripts=converted.scripts,
                )
            )
        except BlockError as exc:
            result.warnings.append(f"{route or '/'}: rejected — {exc}")

    # The template is emitted from the site's own structure, not invented: the
    # home page's shape is the closest thing a replicated site has to a layout.
    result.template = page_template(
        title=f"{site} template",
        route=f"template/{site}",
        blocks=home_blocks or [{"element": "div"}],
        template_group=template_group,
        project_folder=project_folder,
        favicon=favicon,
    )

    everything = [result.template, *result.pages]
    assert_template_emitted(everything)
    result.warnings.extend(check_routes(everything))
    result.prerequisites = prerequisites(everything)
    return result


_PLACEHOLDER: dict[str, Page] = {}


def _placeholder_template(site: str) -> Page:
    """A stand-in so `page()` can enforce its rule while pages are still being built.

    `page()` requires a template, and the real one is derived from the pages —
    so it does not exist yet. This keeps the rule honest rather than adding a
    bypass to `page()` that something else would eventually use to skip the
    template altogether.
    """
    if site not in _PLACEHOLDER:
        _PLACEHOLDER[site] = page_template(
            title=f"{site} template", route=f"template/{site}", blocks=[{"element": "div"}]
        )
    return _PLACEHOLDER[site]


#: Sequences Frappe's Jinja refuses. `safe_render` rejects any template
#: containing `.__` before it even compiles, and the three delimiters make a
#: 360 KB stylesheet a syntax error. One hit = HTTP 417 on every route (#14).
_JINJA_HOSTILE = ("{{", "{%", "{#", ".__")


#: rel="icon"/"shortcut icon" stay out of the head_html lift — that one
#: already becomes `page.favicon`, a single Attach-Image field with no
#: room for the rest of the icon family.
_PRIMARY_ICON_RELS = {"icon", "shortcut icon"}


def _icon_links_for(html: str) -> list[tuple[str, str]]:
    """Every `<link>` in the icon rel family, beyond the primary favicon.

    apple-touch-icon (and its -precomposed variant), mask-icon, and any
    other icon-shaped rel Apple/Safari/PWA markup uses — all skipped by
    the converter as non-content, correctly, since none of them are
    blocks. But each is unmistakably part of what the site looks like on
    the device that reads that specific rel (#16) — `verify`'s browser
    check reported the resulting misses directly ("asset missing from the
    rendered page: icon_180x180_ios…"): the file was fetched by the crawl,
    it just had nothing in the converted page pointing at it.

    Uses `_link_tags` (`crawl.py`) — the same parse `_links()` uses to
    decide what gets fetched — so a link this lifts into `head_html` was
    guaranteed to be one `fetch_assets` actually downloaded (review on
    #16's own PR: an independent, order-dependent regex here once could
    reference a file the crawl's order-independent one never fetched).

    Deduped by `(rel, href)`, not `href` alone: two different rels
    legitimately sharing one file — apple-touch-icon and its -precomposed
    variant pointing at the same PNG is common — must both survive; only
    a *repeated* rel+href pair is a true duplicate.
    """
    links: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for rel, href in _link_tags(html):
        if "icon" not in rel.lower() or rel.lower() in _PRIMARY_ICON_RELS:
            continue
        if href.startswith("data:"):
            continue  # inline; there is no /files/<name> for this to become
        if (rel, href) in seen:
            continue
        seen.add((rel, href))
        links.append((rel, href))
    return links


def _head_html_for(converted, *, site: str, route: str, html: str,
                   assets_dir: Path | None) -> str:
    """Rebuild the head content the source page carried — as references.

    Both the leftover CSS and the page's own scripts belong to the page,
    because that is where the source keeps them — its inline scripts sit
    inside `</head>`.

    Two constraints shape the *how*:

    - `Builder Client Script` records are the "native" home for page JS and
      do not work for a page like this: Builder emits the client-script
      include only from a block whose element is `body`, and a page whose
      root block is a `div` never has one. The scripts were created, linked,
      and silently never rendered — which is how a hover menu and a lightbox
      both went missing while every count looked right (BS-016).
    - Builder renders `head_html` through Frappe's Jinja with `safe_render`,
      which refuses any string containing `.__` — and real head content is
      full of it (`window.__STATE__`, `.__utility-class`). Inlining the
      content 417s every route on the site (#14).

    So the content ships as *files* next to the other crawl assets — the
    loader already publishes that directory at `/files/` — and `head_html`
    carries only reference tags, which are Jinja-inert. The browser executes
    the same bytes either way. Inline emission survives only for asset-less
    conversions, and only when the content is provably Jinja-safe.
    """
    parts = []
    slug = (route or "index").replace("/", "-")
    if assets_dir is not None:
        assets_dir = Path(assets_dir)
        assets_dir.mkdir(parents=True, exist_ok=True)
        if converted.leftover_css:
            name = f"{site}-head-{slug}.css"
            (assets_dir / name).write_text(converted.leftover_css,
                                           encoding="utf-8")
            parts.append(f'<link rel="stylesheet" href="/files/{name}">')
        for n, script in enumerate(converted.scripts):
            name = f"{site}-head-{slug}-{n}.js"
            (assets_dir / name).write_text(script.strip() + "\n",
                                           encoding="utf-8")
            parts.append(f'<script src="/files/{name}"></script>')
        for rel, href in _icon_links_for(html):
            # Not a new file to write — fetch_assets already saved this
            # one under its basename, same convention _css_loader_for
            # matches. Only a reference is missing, not the file.
            name = Path(urllib.parse.unquote(urllib.parse.urlparse(href).path)).name
            if name:
                parts.append(f'<link rel="{rel}" href="/files/{name}">')
        return "\n".join(parts)

    if converted.leftover_css:
        parts.append(f"<style>\n{converted.leftover_css}\n</style>")
    for script in converted.scripts:
        parts.append(f"<script>\n{script.strip()}\n</script>")
    head = "\n".join(parts)
    for marker in _JINJA_HOSTILE:
        if marker in head:
            raise ConversionError(
                f"{route or '/'}: inline head content contains {marker!r}, "
                "which Frappe's safe_render refuses — the page would 417 on "
                "every view (#14). Convert with an assets_dir so the content "
                "ships as files instead."
            )
    return head


def _favicon_for(html: str) -> str | None:
    """The favicon the source page declares.

    It lives in `<link rel="icon">` (or `"shortcut icon"`), which the
    converter skips as non-content — correctly, since it is not a block.
    But it is unmistakably part of what the site looks like, so it is
    lifted out here and set on the page record. Without it the clone
    wears Frappe's logo in the browser tab.

    Scoped to exactly the primary rels (`_PRIMARY_ICON_RELS`), not "any
    rel containing the substring icon": that substring match used to grab
    whichever icon-family link — including an apple-touch-icon — happened
    to appear first in the document, which both set the wrong favicon AND
    made `_icon_links_for` skip the *real* one from `head_html` (believing
    it was already covered). The result was the true favicon referenced
    nowhere at all (review on #16's own PR).
    """
    for rel, href in _link_tags(html):
        if rel.lower() in _PRIMARY_ICON_RELS:
            return href
    return None


def _title_for(route: str, title: str) -> str:
    """The page title, from the parsed document, falling back to the route.

    Takes the already-parsed `<title>` rather than re-finding it with a regex
    over the raw HTML. The regex was a second implementation of the same job and
    it decoded nothing, so a title containing an entity — `&amp;`, `&middot;`,
    anything — reached `page_title` still escaped. The parser has already
    decoded it.
    """
    cleaned = " ".join((title or "").split())
    if cleaned:
        return cleaned
    return route.replace("/", " ").replace("-", " ").strip().title() or "Home"


def emit(result: ReplicateResult, out_dir: str | Path) -> list[Path]:
    """Write the payloads out for someone else to apply."""
    import json

    out = Path(out_dir) / "pages"
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for item in ([result.template] if result.template else []) + result.pages:
        slug = (item.route or "home").replace("/", "_") or "home"
        record = item.record()
        # Client scripts are separate records Builder links to the page, so they
        # travel beside the payload rather than inside it.
        if item.scripts:
            record["_client_scripts"] = item.scripts
        path = out / f"{slug}.json"
        path.write_text(json.dumps(record, indent=2) + "\n")
        written.append(path)
    return written
