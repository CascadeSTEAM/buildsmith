"""Crawl a source site into files, for W1 to convert.

The predecessor's crawler did one page. W1's success floor is *all original
content present, routes preserved*, so this does the whole site — and, just as
importantly, says what it did not reach. A crawl that quietly stops at 50 pages
produces a replication that is quietly missing the rest.

**It fetches content, never behaviour.** HTML and the images the pages
reference; scripts are recorded and left alone. A replication is a copy of what
a site *says*, not of how it works — but an image is content. A clone whose
`<img>` tags all 404 is not a clone, it is a wireframe.

**robots.txt is honoured by default.** Usually you are crawling a site you
control, which is why `--ignore-robots` exists — but that has to be a decision
someone makes, not a default nobody noticed.

**No silent truncation.** Every limit that bites is reported. `max_pages`,
a fetch error, a skipped content type, a disallowed path: all of it lands in
`CrawlResult.skipped`, so "we replicated the site" can be checked rather than
believed.

Local mode takes a directory of HTML files instead, which is how the tests run
and how you re-convert a crawl without hitting the network again.
"""

from __future__ import annotations

import contextlib
import re
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from pathlib import Path

from buildsmith.errors import CouldNotCheck

__all__ = ["CrawlResult", "crawl_local", "crawl_site", "fetch_assets", "route_for", "save_crawl"]

USER_AGENT = "buildsmith-replicate/0.1 (+site replication; contact the site owner)"


@dataclass
class CrawlResult:
    """Pages fetched, and an account of everything that was not."""

    pages: dict[str, str] = field(default_factory=dict)  # route -> html
    skipped: list[str] = field(default_factory=list)
    assets: set[str] = field(default_factory=set)
    #: url -> local filename, for assets actually downloaded.
    downloaded: dict[str, str] = field(default_factory=dict)
    truncated: bool = False
    #: url -> bytes, for assets a --render crawl already saw the browser
    #: fetch successfully (#12). Signed/protected CDN assets can 403 on a
    #: cold static refetch even though the browser just loaded them —
    #: fetch_assets prefers these over hitting the network a second time.
    captured: dict[str, bytes] = field(default_factory=dict)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "pages": len(self.pages),
            "skipped": len(self.skipped),
            "assets_referenced": len(self.assets),
            "assets_downloaded": len(self.downloaded),
        }

    def summary(self) -> str:
        lines = [
            f"crawl: {len(self.pages)} page(s), {len(self.assets)} asset(s) referenced, "
            f"{len(self.downloaded)} downloaded"
        ]
        missing = len(self.assets) - len(self.downloaded)
        if missing > 0:
            lines.append(
                f"  {missing} asset(s) referenced but NOT downloaded — every one of those "
                "is a broken image in the clone. Pass fetch_assets=True."
            )
        if self.truncated:
            lines.append(
                "  TRUNCATED — the page limit was reached, so this crawl is incomplete. "
                "Raise max_pages; a partial crawl replicates a partial site."
            )
        for note in self.skipped:
            lines.append(f"  skipped: {note}")
        return "\n".join(lines)


def route_for(url: str, base: str) -> str:
    """The route a URL becomes, relative to the site root.

    `/about/`, `/about/index.html` and `/about` are the same page; normalising
    them apart would replicate it three times and give two of them the wrong
    route.
    """
    path = urllib.parse.urlparse(urllib.parse.urljoin(base, url)).path
    if path.endswith("/index.html"):
        path = path[: -len("index.html")]
    # A `.html` suffix is a filename, not a route. Sites serve `/menu`, and a
    # crawl saved to disk as `menu.html` must read back as `menu` — otherwise
    # the round trip through the filesystem silently republishes every page at
    # a URL the source site never had, which is W1's success floor violated in
    # the least visible way possible. Found dogfooding a real site.
    for suffix in (".html", ".htm"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return path.strip("/")


def _same_origin(url: str, base: str) -> bool:
    a, b = urllib.parse.urlparse(url), urllib.parse.urlparse(base)
    return (a.scheme, a.netloc) == (b.scheme, b.netloc)


def _link_tags(html: str) -> list[tuple[str, str]]:
    """Every `<link>` tag's `(rel, href)` pair, attribute-order independent.

    Matched tag-first, then `rel`/`href` searched for independently within
    it, so `href` before `rel` — legal HTML, and real markup — isn't
    silently missed the way a single ordered regex would miss it (#16's
    review: the icon-discovery regex this replaces required `rel` first,
    so a same-tag reference build.py assembled from a more tolerant parser
    could point at a file this function never fetched).

    The one place that decides what a `<link>` tag *is*, for both crawling
    (this module) and referencing it back (`build.py`'s `_favicon_for`/
    `_icon_links_for`) — so the two can't silently disagree about which
    links exist.
    """
    pairs = []
    for tag_match in re.finditer(r"<link\b[^>]*>", html, re.I):
        tag = tag_match.group(0)
        rel = re.search(r"""\brel\s*=\s*["']([^"']*)""", tag, re.I)
        href = re.search(r"""\bhref\s*=\s*["']([^"']+)""", tag, re.I)
        if rel and href:
            pairs.append((rel.group(1).strip(), href.group(1)))
    return pairs


def _links(html: str, base: str) -> tuple[set[str], set[str]]:
    """Return (page links, asset references). Deliberately crude and tolerant.

    An asset is anything the page needs in order to *look right*, and they hide
    in four different places. Collecting only `<img src>` — the obvious one —
    left a hero background and a favicon missing from an otherwise complete
    clone, with the CSS rule faithfully recovered and pointing at a file that
    was never fetched. Found by looking at the page and noticing the hero was
    blank.
    """
    import re

    pages, assets = set(), set()

    for match in re.finditer(r"""<a\b[^>]*\bhref\s*=\s*["']([^"'#]+)""", html, re.I):
        pages.add(urllib.parse.urljoin(base, match.group(1)))

    # 1. media elements
    for match in re.finditer(
        r"""<(?:img|source|video|audio)\b[^>]*\bsrc\s*=\s*["']([^"']+)""", html, re.I
    ):
        assets.add(urllib.parse.urljoin(base, match.group(1)))

    # 2. srcset, which carries the responsive variants
    for match in re.finditer(r"""\bsrcset\s*=\s*["']([^"']+)""", html, re.I):
        for candidate in match.group(1).split(","):
            url = candidate.strip().split()[0] if candidate.strip() else ""
            if url:
                assets.add(urllib.parse.urljoin(base, url))

    # 3. icons — the favicon (and the rest of the icon rel family: touch
    #    icons, mask icons) lives in <link>, which is not rendered content
    #    but is unmistakably part of what the site looks like.
    # 4. stylesheets — on most real sites the appearance lives in linked CSS,
    #    and style recovery reads it (htmlblocks css_loader). A stylesheet
    #    never fetched is a page converted with no appearance at all.
    for rel, href in _link_tags(html):
        rel_tokens = rel.lower().split()
        if href.startswith("data:"):
            continue  # inline; nothing to fetch, nothing to save under a name
        if "icon" in rel.lower() or "stylesheet" in rel_tokens:
            assets.add(urllib.parse.urljoin(base, href))

    # 5. url(...) anywhere in CSS — <style> blocks and inline style attributes.
    #    This is where background images live, and they are frequently the
    #    largest visual element on the page.
    for match in re.finditer(r"""url\(\s*["']?([^"')]+)""", html, re.I):
        url = match.group(1).strip()
        if url and not url.startswith("data:"):
            assets.add(urllib.parse.urljoin(base, url))

    return pages, assets


def save_crawl(result: CrawlResult, directory: str | Path) -> None:
    """Write crawled pages to disk the way crawl_local reads them back.

    Routes nest (`s/gallery`), so every write makes its parents first — the
    first nested route ever crawled crashed the clone for want of a mkdir.
    """
    root = Path(directory)
    for route, html in result.pages.items():
        path = root / ((route or "index") + ".html")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")


def crawl_local(directory: str | Path) -> CrawlResult:
    """Read a directory of HTML files as if it were a crawled site."""
    root = Path(directory)
    if not root.is_dir():
        raise ValueError(f"{root} is not a directory")

    result = CrawlResult()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".html", ".htm"}:
            result.assets.add(str(path.relative_to(root)))
            continue
        relative = path.relative_to(root).as_posix()
        route = route_for("/" + relative, "http://local/")
        result.pages[route] = path.read_text(encoding="utf-8", errors="replace")

    if not result.pages:
        raise ValueError(
            f"{root} contains no .html files. An empty crawl replicates an empty site, "
            "which would pass every downstream check and deliver nothing."
        )
    return result


def fetch_assets(
    result: CrawlResult, into: str | Path, *, timeout: float = 20.0
) -> CrawlResult:
    """Download every referenced asset. A clone with broken images is not a clone.

    Filenames are preserved exactly, because the pages reference them by name —
    rewriting them would mean rewriting every `src` in every block, and the
    whole point is that the markup already points at the right place.
    """
    directory = Path(into)
    directory.mkdir(parents=True, exist_ok=True)

    for url in sorted(result.assets):
        name = urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1])
        if not name:
            result.skipped.append(f"{url}: no filename to save it under")
            continue
        target = directory / name
        if target.exists():
            result.downloaded[url] = name
            continue
        captured = result.captured.get(url)
        if captured is not None:
            # The browser already fetched this during a --render crawl —
            # use those bytes rather than hitting the network cold a second
            # time. A signed/protected CDN asset can 403 on that refetch
            # even though the browser had just loaded it moments earlier.
            try:
                target.write_bytes(captured)
                result.downloaded[url] = name
            except Exception as exc:  # noqa: BLE001 - a failed asset is a reportable gap
                result.skipped.append(f"{url}: asset not downloaded — {exc}")
            continue
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                target.write_bytes(response.read())
            result.downloaded[url] = name
        except Exception as exc:  # noqa: BLE001 - a failed asset is a reportable gap
            result.skipped.append(f"{url}: asset not downloaded — {exc}")
    return result


def crawl_site(
    base_url: str,
    *,
    max_pages: int = 200,
    ignore_robots: bool = False,
    timeout: float = 15.0,
    opener=None,
    render: bool = False,
) -> CrawlResult:
    """Crawl a site breadth-first, same origin only.

    `opener` is injectable so the tests can run without a network. `render`
    fetches every page through a real browser instead of plain HTTP — the
    only honest way to crawl a site that assembles itself client-side.
    """
    if render and opener is None:
        with _rendered_fetcher(timeout) as (fetch, captured):
            result = _crawl(base_url, fetch, max_pages=max_pages,
                            ignore_robots=ignore_robots, timeout=timeout,
                            render=True)
            # Merge while the browser is still open (the `with` hasn't torn
            # it down yet) — `captured` is the same dict the response
            # listener has been filling in throughout the crawl.
            result.captured.update(captured)
            return result
    return _crawl(base_url, opener or _fetch, max_pages=max_pages,
                  ignore_robots=ignore_robots, timeout=timeout, render=render)


def _crawl(
    base_url: str,
    fetch,
    *,
    max_pages: int,
    ignore_robots: bool,
    timeout: float,
    render: bool,
) -> CrawlResult:
    result = CrawlResult()

    robots = None
    if not ignore_robots:
        robots = urllib.robotparser.RobotFileParser()
        robots.set_url(urllib.parse.urljoin(base_url, "/robots.txt"))
        try:
            robots.read()
        except Exception:  # noqa: BLE001 - absent or unreadable robots.txt means no rules
            robots = None

    queue = [base_url]
    seen: set[str] = set()

    while queue:
        if len(result.pages) >= max_pages:
            result.truncated = True
            result.skipped.append(
                f"stopped at max_pages={max_pages} with {len(queue)} url(s) still queued"
            )
            break

        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)

        if not _same_origin(url, base_url):
            continue
        if robots is not None and not robots.can_fetch(USER_AGENT, url):
            result.skipped.append(f"{url}: disallowed by robots.txt")
            continue

        try:
            content_type, body = fetch(url, timeout)
        except Exception as exc:  # noqa: BLE001 - any fetch failure is a reportable gap
            result.skipped.append(f"{url}: fetch failed — {exc}")
            continue

        if "html" not in content_type.lower():
            result.assets.add(url)
            continue

        result.pages[route_for(url, base_url)] = body
        page_links, assets = _links(body, url)
        result.assets |= assets
        queue += [link for link in sorted(page_links) if link not in seen]

    if not result.pages:
        raise ValueError(
            f"{base_url}: nothing was crawled. Check the URL and robots.txt — an empty "
            "crawl would pass downstream as a successfully replicated empty site."
        )

    # The success-shaped failure (#5): a client-side-rendered site serves a
    # bootstrap shell, the crawl "succeeds", and the replication converts a
    # husk at 100%. If every page looks like a shell, this crawl proved
    # nothing — refuse rather than hand downstream a confident husk.
    shells = [route for route, html in result.pages.items() if _looks_like_shell(html)]
    if shells and len(shells) == len(result.pages):
        if render:
            raise CouldNotCheck(
                f"{base_url}: even the rendered crawl came back looking like an "
                "empty shell (all pages: heavy markup, several scripts, almost "
                "no visible text). The site may need interaction or a longer "
                "settle; nothing here is safe to convert."
            )
        raise CouldNotCheck(
            f"{base_url}: every crawled page looks like a JavaScript bootstrap "
            "shell — heavy markup, several scripts, almost no visible text. A "
            "static fetch cannot see this site's content. Retry with --render, "
            "which crawls what a real browser renders."
        )
    return result


#: A page is shell-suspect when it is all machinery and no words: kilobytes of
#: markup, a pile of scripts, and less visible text than a business card. The
#: thresholds come from the husk that motivated this (51 KB, 15 scripts, 22
#: visible characters) with a wide safety margin against real minimal pages.
_SHELL_MAX_VISIBLE = 200
_SHELL_MIN_SCRIPTS = 3
_SHELL_MIN_BYTES = 5000


def _visible_text(html: str) -> str:
    stripped = re.sub(r"<(script|style|noscript|template)\b.*?</\1>", " ", html,
                      flags=re.S | re.I)
    stripped = re.sub(r"<[^>]+>", " ", stripped)
    return " ".join(stripped.split())


def _looks_like_shell(html: str) -> bool:
    return (
        len(html) >= _SHELL_MIN_BYTES
        and len(re.findall(r"<script\b", html, re.I)) >= _SHELL_MIN_SCRIPTS
        and len(_visible_text(html)) < _SHELL_MAX_VISIBLE
    )


#: Response types worth capturing bytes for. Fonts/stylesheets are cheap
#: insurance; the reported failure (#12) is CDN-protected images.
_CAPTURED_RESOURCE_TYPES = frozenset({"image", "media", "font", "stylesheet"})

#: Best-effort cap on total captured bytes for one render session (review
#: on #12's own PR): unreferenced media the browser loads but no page ever
#: links to (a carousel slide, a hover state) would otherwise accumulate
#: for the whole multi-page crawl with nothing to evict it. Hitting this
#: just means some assets fall back to fetch_assets' static refetch —
#: exactly pre-#12 behaviour, not a new failure mode.
_MAX_CAPTURED_BYTES = 200 * 1024 * 1024


def _should_capture(ok: bool, resource_type: str) -> bool:
    """A pure predicate, factored out so the filter is testable without a
    real (or faked) playwright response object."""
    return ok and resource_type in _CAPTURED_RESOURCE_TYPES


def _capture_response(response, captured: dict[str, bytes], captured_bytes: int) -> int:
    """Register `response`'s body into `captured`, keyed by every url in
    its redirect chain. Returns the updated running total of captured bytes.

    A signed CDN typically 302s a stable url to a signed one; the browser's
    own `response.url` is the signed (final) url, but the HTML — and
    `result.assets`, and `fetch_assets`' own lookup — reference the
    ORIGINAL, pre-redirect url. Capturing only the final url means the
    lookup never hits for exactly the case #12 reports (review on #12's
    own PR), so this walks `request.redirected_from` back through the
    whole chain.

    Takes and returns `captured_bytes` rather than closing over it, so the
    redirect-walk and the size cap are testable with plain fake objects —
    no real (or faked) playwright Response needed.
    """
    if not _should_capture(response.ok, response.request.resource_type):
        return captured_bytes
    if captured_bytes >= _MAX_CAPTURED_BYTES:
        return captured_bytes
    body = response.body()
    request, seen = response.request, set()
    while request is not None and request.url not in seen:
        if request.url not in captured:
            captured_bytes += len(body)
        captured[request.url] = body
        seen.add(request.url)
        request = request.redirected_from
    return captured_bytes


@contextlib.contextmanager
def _rendered_fetcher(timeout: float):
    """One browser for the whole crawl, yielded as (fetch, captured).

    `fetch(url, timeout)` is the page-navigation callable `_crawl` already
    expects. `captured` is a `{url: bytes}` dict a response listener fills
    in throughout the session — every asset the browser actually loaded
    while rendering a page, so `fetch_assets` doesn't have to refetch a
    signed/protected CDN url cold and get a 403 for something the browser
    had just loaded moments earlier (and doesn't hit the source's CDN a
    second time either). Missing playwright refuses (exit 2) rather than
    quietly degrading to the static fetch — a degraded crawl is exactly the
    husk the shell tripwire exists to catch, one layer too late.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise CouldNotCheck(
            "--render needs playwright with Chromium (install.py --dev sets "
            "both up). Refusing to fall back to the static fetch: that is the "
            "exact crawl --render exists to avoid."
        ) from exc

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(user_agent=USER_AGENT)
        captured: dict[str, bytes] = {}
        captured_bytes = 0

        def _on_response(response) -> None:
            # Best-effort: a response this can't read just falls through to
            # fetch_assets' own static-refetch fallback, same as today.
            nonlocal captured_bytes
            try:
                captured_bytes = _capture_response(response, captured, captured_bytes)
            except Exception:  # noqa: BLE001 - never let a listener crash the crawl
                pass

        page.on("response", _on_response)
        try:
            def fetch(url: str, _timeout: float) -> tuple[str, str]:
                page.goto(url, wait_until="networkidle",
                          timeout=int(timeout * 1000))
                return "text/html", page.content()

            yield fetch, captured
        finally:
            browser.close()


def _fetch(url: str, timeout: float) -> tuple[str, str]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        content_type = response.headers.get("Content-Type", "")
        charset = response.headers.get_content_charset() or "utf-8"
        return content_type, response.read().decode(charset, errors="replace")
