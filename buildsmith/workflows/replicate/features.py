"""Derive a feature inventory from a source site.

The point of this file is that **nobody writes the list**. A hand-written
checklist only ever contains the features someone remembered, and the whole
problem is the ones nobody noticed: a hover menu, a lightbox, a hero background.
Each of those survived a clone, and each was found by a human looking at the
page rather than by any check.

So the inventory is *extracted* from the source. If the source has a click
handler, the inventory has an entry for it, whether or not anyone knew it was
there. Then the same list is checked against the clone, and later against the
live site after publishing — so "works on my machine" becomes a claim something
verifies rather than a thing someone says.

What it records per route:

- **assets** every URL the page needs in order to look right
- **text landmarks** headings and distinctive runs, so wording cannot vanish
- **links** and their targets, so navigation cannot quietly break
- **interactions** derived from the scripts: what event, on what selector, and
  what it is expected to do
- **runtime elements** ids a script creates that do not exist in the markup
- **breakpoints** the media queries the source actually defines

Nothing here touches a site; it reads crawled HTML.
"""

from __future__ import annotations

import html as html_module
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["Feature", "Inventory", "extract", "extract_site"]

#: Events worth checking by driving a browser. A handler bound to one of these
#: is a feature a user can notice the absence of.
INTERACTIVE_EVENTS = (
    "click", "mouseenter", "mouseover", "mouseleave", "mouseout",
    "focus", "submit", "change", "input", "keydown", "scroll",
)


@dataclass
class Feature:
    kind: str
    detail: str
    selector: str = ""
    evidence: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v}


@dataclass
class Inventory:
    site: str
    routes: dict[str, list[dict]] = field(default_factory=dict)
    breakpoints: list[int] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.routes.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "_meta": {
                "site": self.site,
                "note": (
                    "Extracted from the source, not hand-written. Every entry is "
                    "something the source does; the clone and, after publishing, the "
                    "live site are checked against this list."
                ),
                "features": self.total,
            },
            "breakpoints": self.breakpoints,
            "routes": self.routes,
        }

    def write(self, path: str | Path) -> Path:
        path = Path(path)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n")
        return path

    def summary(self) -> str:
        lines = [f"feature inventory for '{self.site}': {self.total} across "
                 f"{len(self.routes)} route(s)"]
        for route, features in sorted(self.routes.items()):
            kinds: dict[str, int] = {}
            for feature in features:
                kinds[feature["kind"]] = kinds.get(feature["kind"], 0) + 1
            listing = ", ".join(f"{v} {k}" for k, v in sorted(kinds.items()))
            lines.append(f"  /{route or ''}: {listing}")
        if self.breakpoints:
            lines.append(f"  breakpoints: {self.breakpoints}")
        return "\n".join(lines)


def _scripts(html: str) -> list[str]:
    return re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S | re.I)


def extract(html: str, route: str) -> list[Feature]:
    """Everything this page does that a clone could silently fail to reproduce."""
    features: list[Feature] = []

    # --- assets: anything the page needs to look right ----------------------
    assets = set(re.findall(r'<img[^>]+src=["\']([^"\']+)', html, re.I))
    assets |= {
        u for u in re.findall(r"url\(\s*[\"']?([^\"')]+)", html) if not u.startswith("data:")
    }
    assets |= set(
        re.findall(r'<link[^>]*rel=["\'][^"\']*icon[^"\']*["\'][^>]*href=["\']([^"\']+)',
                   html, re.I)
    )
    for url in sorted(a for a in assets if a.strip()):
        features.append(Feature("asset", f"{url} must load", evidence=url))

    # --- text landmarks -----------------------------------------------------
    for level in ("h1", "h2", "h3"):
        for match in re.finditer(rf"<{level}[^>]*>(.*?)</{level}>", html, re.S | re.I):
            # Unescape: the inventory is compared against what a browser
            # renders, and innerText gives "Tea & Toast", not the source's
            # "Tea &amp; Toast".
            text = html_module.unescape(
                " ".join(re.sub(r"<[^>]+>", " ", match.group(1)).split())
            )
            if text:
                features.append(Feature("heading", text[:120], selector=level))

    # --- navigation ---------------------------------------------------------
    for href, label in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                                  html, re.S | re.I):
        text = html_module.unescape(" ".join(re.sub(r"<[^>]+>", " ", label).split()))
        if text:
            features.append(Feature("link", f"{text[:60]} -> {href}", evidence=href))

    # --- forms --------------------------------------------------------------
    for match in re.finditer(r"<form[^>]*>", html, re.I):
        features.append(Feature("form", match.group(0)[:100], selector="form"))

    # --- behaviour, read out of the page's own scripts ----------------------
    for script in _scripts(html):
        for event in set(re.findall(r"""addEventListener\(\s*["'](\w+)["']""", script)):
            if event in INTERACTIVE_EVENTS:
                features.append(
                    Feature("interaction", f"an element responds to '{event}'",
                            evidence=event)
                )

        # Elements a script creates at runtime — invisible in the markup, and
        # exactly what a lightbox is.
        for created in set(re.findall(r"""id\s*=\s*["']([\w-]+)["']""", script)) | set(
            re.findall(r"""createElement\([^)]*\)[\s\S]{0,120}?\.id\s*=\s*["']([\w-]+)""", script)
        ):
            features.append(
                Feature("runtime-element", f"a script creates #{created}",
                        selector=f"#{created}")
            )
        for target in set(re.findall(r"""getElementById\(\s*["']([\w-]+)["']""", script)):
            features.append(
                Feature("runtime-element", f"a script manages #{target}",
                        selector=f"#{target}")
            )
        for selector in set(re.findall(r"""querySelectorAll?\(\s*["']([^"']+)["']""", script)):
            features.append(
                Feature("script-target", f"a script queries {selector}", selector=selector)
            )

    # De-duplicate while keeping order, so the list reads as a checklist.
    seen, unique = set(), []
    for feature in features:
        key = (feature.kind, feature.detail, feature.selector)
        if key in seen:
            continue
        seen.add(key)
        unique.append(feature)
    return unique


def extract_site(crawl_dir: str | Path, *, site: str) -> Inventory:
    """Build the inventory for a whole crawled site."""
    root = Path(crawl_dir)
    inventory = Inventory(site=site)
    breakpoints: set[int] = set()

    # rglob, not glob: nested routes land as subdirectories, and a flat scan
    # silently dropped them from the inventory — the checklist then contained
    # only the top-level pages, which is precisely the failure features.json
    # exists to prevent.
    for path in sorted(root.rglob("*.html")):
        html = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(root).with_suffix("")
        if rel.name == "index":
            rel = rel.parent
        route = "" if str(rel) == "." else rel.as_posix()
        inventory.routes[route] = [f.as_dict() for f in extract(html, route)]
        for width in re.findall(r"max-width:\s*(\d+)", html):
            breakpoints.add(int(width))

    inventory.breakpoints = sorted(breakpoints)
    return inventory
