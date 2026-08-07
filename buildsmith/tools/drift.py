#!/usr/bin/env python3
"""Has the live site changed since we cloned it?

You clone, you edit for a week, you publish. If somebody changed the live site
in the meantime, an overwrite silently reverts their work and a merge is
guesswork. Neither is detectable from the payloads — the payloads only know what
*we* did.

So this re-fetches the source and compares it against the crawl the clone was
built from. It answers one question: **what is on live now that was not there
when we took our copy?**

    buildsmith drift --site example --source https://example.test/

Exit status:
  0  no drift — the live site matches the crawl, publishing is a clean decision
  1  drift found — read it before choosing merge or overwrite
  2  could not check — treated as drift, because an unchecked assumption about
     a live site is not the same as a checked one

Nothing here writes anywhere.
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from buildsmith.errors import CouldNotCheck

ROOT = Path(__file__).resolve().parents[2]
USER_AGENT = "buildsmith-drift/0.1"

__all__ = ["Drift", "check", "main"]


@dataclass
class Drift:
    routes_added: list[str] = field(default_factory=list)
    routes_removed: list[str] = field(default_factory=list)
    changed: dict[str, list[str]] = field(default_factory=dict)
    unreachable: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not any(
            (self.routes_added, self.routes_removed, self.changed, self.unreachable)
        )


def _fetch(url: str, timeout: float = 30.0) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def _signature(html: str) -> dict[str, set[str]]:
    """The parts of a page worth noticing a change in."""
    css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S | re.I))
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", "\n", text)
    return {
        "text": {" ".join(t.split()) for t in text.splitlines() if t.strip()},
        "declarations": {
            " ".join(d.split())
            for _, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
            for d in body.split(";")
            if ":" in d
        },
        "assets": {
            u for u in
            set(re.findall(r'<img[^>]+src=["\']([^"\']+)', html, re.I))
            | {x for x in re.findall(r"url\(\s*[\"']?([^\"')]+)", html)
               if not x.startswith("data:")}
        },
        "links": set(re.findall(r'<a[^>]+href=["\']([^"\']+)', html, re.I)),
    }


def check(site: str, source: str) -> Drift:
    crawl_dir = ROOT / "sites" / site / "crawl"
    if not crawl_dir.is_dir():
        raise CouldNotCheck(
            f"{crawl_dir} does not exist — there is no crawl to compare against, so "
            "'has live changed?' has no answer. Clone first."
        )

    drift = Drift()
    base = source.rstrip("/")

    crawled = {}
    for path in sorted(crawl_dir.glob("*.html")):
        route = "" if path.stem == "index" else path.stem
        crawled[route] = path.read_text(encoding="utf-8", errors="replace")

    for route, old_html in crawled.items():
        url = f"{base}/{route}"
        try:
            now_html = _fetch(url)
        except Exception as exc:  # noqa: BLE001
            drift.unreachable.append(f"{url}: {exc}")
            continue

        old, new = _signature(old_html), _signature(now_html)
        differences = []
        for part in ("text", "declarations", "assets", "links"):
            added = new[part] - old[part]
            removed = old[part] - new[part]
            if added:
                differences += [f"+ {part}: {a[:90]}" for a in sorted(added)[:5]]
            if removed:
                differences += [f"- {part}: {r[:90]}" for r in sorted(removed)[:5]]
        if differences:
            drift.changed[route or "/"] = differences

    # A route that appeared on live since the crawl would be silently destroyed
    # by an overwrite, so look for links pointing somewhere we never cloned.
    try:
        home = _fetch(base + "/")
        linked = {
            re.sub(r"[#?].*$", "", h).strip("/")
            for h in re.findall(r'<a[^>]+href=["\'](/[^"\']*)', home, re.I)
        }
        drift.routes_added = sorted(r for r in linked if r and r not in crawled)
    except Exception as exc:  # noqa: BLE001
        drift.unreachable.append(f"{base}/: {exc}")

    return drift


def report(drift: Drift, source: str, site: str) -> None:
    """Print the finding. Split out of main() so the CLI and the script agree —
    two renderings of the same result is two chances to disagree about it."""
    print(f"drift check: {source} vs the crawl behind sites/{site}/\n")

    if drift.unreachable:
        print("COULD NOT CHECK — treated as drift, not as absence of drift:")
        for item in drift.unreachable:
            print(f"  {item}")
        print()
        return

    if drift.routes_added:
        print(f"routes on live that we never cloned — {len(drift.routes_added)}")
        for route in drift.routes_added:
            print(f"  /{route}  (an overwrite would not create this; it would remain, "
                  "unmanaged)")
        print()

    for route, differences in sorted(drift.changed.items()):
        print(f"{route} — {len(differences)} change(s) since the crawl")
        for line in differences[:8]:
            print(f"  {line}")
        print()

    if drift.clean:
        print("No drift. The live site is what we cloned, so merge-versus-overwrite")
        print("is a choice about your edits alone.")
        return

    print("Live has moved since the crawl. An overwrite would revert everything above.")
    print("Re-clone, or merge deliberately — do not let the publish decide.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--site", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    drift = check(args.site, args.source)
    if args.json:
        print(json.dumps(drift.__dict__, indent=2, default=list))
    else:
        report(drift, args.source, args.site)
    if drift.unreachable:
        return 2
    return 0 if drift.clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
