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

__all__ = ["CrawlResult", "crawl_local", "crawl_site", "fetch_assets", "route_for"]

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

    # 3. icons — the favicon lives in <link>, which is not rendered content
    #    but is unmistakably part of what the site looks like.
    for match in re.finditer(
        r"""<link\b[^>]*\brel\s*=\s*["'][^"']*icon[^"']*["'][^>]*\bhref\s*=\s*["']([^"']+)""",
        html, re.I,
    ):
        assets.add(urllib.parse.urljoin(base, match.group(1)))

    # 4. stylesheets — on most real sites the appearance lives in linked CSS,
    #    and style recovery reads it (htmlblocks css_loader). A stylesheet
    #    never fetched is a page converted with no appearance at all. Matched
    #    tag-first so attribute order cannot hide one (rel before href and
    #    href before rel are both legal).
    for tag_match in re.finditer(r"<link\b[^>]*>", html, re.I):
        tag = tag_match.group(0)
        rel = re.search(r"""\brel\s*=\s*["']([^"']*)""", tag, re.I)
        href = re.search(r"""\bhref\s*=\s*["']([^"']+)""", tag, re.I)
        if rel and href and "stylesheet" in rel.group(1).lower().split():
            assets.add(urllib.parse.urljoin(base, href.group(1)))

    # 5. url(...) anywhere in CSS — <style> blocks and inline style attributes.
    #    This is where background images live, and they are frequently the
    #    largest visual element on the page.
    for match in re.finditer(r"""url\(\s*["']?([^"')]+)""", html, re.I):
        url = match.group(1).strip()
        if url and not url.startswith("data:"):
            assets.add(urllib.parse.urljoin(base, url))

    return pages, assets


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
        with _rendered_fetcher(timeout) as fetch:
            return _crawl(base_url, fetch, max_pages=max_pages,
                          ignore_robots=ignore_robots, timeout=timeout,
                          render=True)
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


@contextlib.contextmanager
def _rendered_fetcher(timeout: float):
    """One browser for the whole crawl, yielded as a fetch(url, timeout) callable.

    Missing playwright refuses (exit 2) rather than quietly degrading to the
    static fetch — a degraded crawl is exactly the husk the shell tripwire
    exists to catch, one layer too late.
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
        try:
            def fetch(url: str, _timeout: float) -> tuple[str, str]:
                page.goto(url, wait_until="networkidle",
                          timeout=int(timeout * 1000))
                return "text/html", page.content()

            yield fetch
        finally:
            browser.close()


def _fetch(url: str, timeout: float) -> tuple[str, str]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        content_type = response.headers.get("Content-Type", "")
        charset = response.headers.get_content_charset() or "utf-8"
        return content_type, response.read().decode(charset, errors="replace")
