#!/usr/bin/env python3
"""Compare a clone against its source, by content. Not by counts.

This exists because counting was not good enough and shipped a clone with a
missing hover state and a dead lightbox while reporting "within one declaration
of live". Totals hide omissions perfectly: 375 of 376 declarations can be 375
*different* declarations, and the number looks like success.

So every check here is a **set difference**, and the report is the list of
things present in the source and absent from the clone:

- CSS **selectors**, and for shared selectors, the **declarations** inside them
- **asset URLs** the page references, and whether each one actually resolves
- **text**, compared as normalised runs so wording cannot silently vanish
- **links** and their targets
- **scripts**, by content

Exit status is 1 if the clone is missing anything. There is no "close enough".

    bin/clone-diff.py --source https://example.test/ --clone http://127.0.0.1:8000/
    bin/clone-diff.py --source https://example.test/menu --clone http://127.0.0.1:8000/menu
"""

from __future__ import annotations

import argparse
import re
import subprocess
import urllib.request
from dataclasses import dataclass, field

USER_AGENT = "buildsmith-clone-diff/0.1"

__all__ = ["Comparison", "compare", "fetch", "main"]


def fetch(url: str, timeout: float = 30.0) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def _css(html: str) -> str:
    return "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S | re.I))


#: Builder mints a class name per distinct style bundle, hashed per site. The
#: same styles therefore get different names on two sites, and comparing names
#: reports every rule as missing — which is a false alarm loud enough to bury a
#: real one. Compare what these rules *say*, not what they are called.
GENERATED_CLASS = re.compile(r"^\.(?:fb|bldr)-[0-9a-f]{6,}$")

#: Builder stores `font-family` as ONE family name, not a stack — its editor
#: cannot parse anything else (see `_primary_font_family` in the converter). So
#: importing a site that used a stack necessarily reduces it, and every rule
#: containing a font then differs from the source by that one declaration.
#:
#: On this site that was 338 differences on a single page. Reporting them as
#: omissions would bury every real finding, which is the failure BS-007 already
#: taught us. Silently normalising them away would be worse: in the *maintain*
#: scenario a font change IS a defect, and a converter bug that kept the wrong
#: family would look identical to the intended reduction.
#:
#: So: a bundle that matches only after reduction is counted separately and
#: reported as an intended transformation. Anything else stays a real finding —
#: including `Skybald, Merriline` -> `Merriline`, because the reduced source is
#: `Skybald` and that still does not match.
_FONT_DECL = re.compile(r"^font-family\s*:\s*(.+)$", re.I)


def _reduce_font_stacks(bundle: frozenset[str]) -> frozenset[str]:
    out = set()
    for declaration in bundle:
        match = _FONT_DECL.match(declaration.strip())
        if match:
            # CSS escapes spaces in unquoted family names as `\ `; undo that
            # before splitting, or the first family keeps a trailing backslash.
            stack = match.group(1).replace("\\ ", " ")
            first = stack.split(",")[0].strip().strip("'\"")
            out.add(f"font-family: {first}")
        else:
            out.add(declaration)
    return frozenset(out)


def _rules(html: str) -> dict[str, set[str]]:
    """selector -> set of `property: value`. Media queries keep their context."""
    css = _css(html)
    out: dict[str, set[str]] = {}

    def add(selector: str, body: str, context: str = "") -> None:
        selector = " ".join(selector.split())
        if not selector or selector.startswith("@"):
            return
        key = f"{context}{selector}" if context else selector
        declarations = {
            " ".join(d.split()).rstrip(";")
            for d in body.split(";")
            if ":" in d and d.strip()
        }
        if declarations:
            out.setdefault(key, set()).update(declarations)

    index = 0
    while True:
        at = css.find("@media", index)
        if at == -1:
            break
        brace = css.find("{", at)
        condition = " ".join(css[at:brace].split())
        depth, cursor = 1, brace + 1
        while cursor < len(css) and depth:
            depth += (css[cursor] == "{") - (css[cursor] == "}")
            cursor += 1
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css[brace + 1 : cursor - 1]):
            add(selector, body, context=f"{condition} ")
        css = css[:at] + css[cursor:]
        index = at

    for selector, body in re.findall(r"([^{}@]+)\{([^{}]*)\}", css):
        add(selector, body)
    return out


def _assets(html: str) -> set[str]:
    found = set(re.findall(r'<img[^>]+src=["\']([^"\']+)', html, re.I))
    found |= {
        u for u in re.findall(r"url\(\s*[\"']?([^\"')]+)", html) if not u.startswith("data:")
    }
    found |= set(
        re.findall(r'<link[^>]*rel=["\'][^"\']*icon[^"\']*["\'][^>]*'
                   r'href=["\']([^"\']+)', html, re.I)
    )
    return {u.strip() for u in found if u.strip()}


def _text(html: str) -> set[str]:
    stripped = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    stripped = re.sub(r"<[^>]+>", "\n", stripped)
    return {" ".join(line.split()) for line in stripped.splitlines() if line.strip()}


def _links(html: str) -> set[str]:
    return {h for h in re.findall(r'<a[^>]+href=["\']([^"\']+)', html, re.I)}


def _scripts(html: str) -> set[str]:
    bodies = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S | re.I)
    return {" ".join(b.split()) for b in bodies if b.strip()}


@dataclass
class Comparison:
    missing_selectors: list[str] = field(default_factory=list)
    changed_selectors: dict[str, list[str]] = field(default_factory=dict)
    missing_assets: list[str] = field(default_factory=list)
    unresolvable_assets: list[str] = field(default_factory=list)
    missing_text: list[str] = field(default_factory=list)
    missing_links: list[str] = field(default_factory=list)
    missing_scripts: list[str] = field(default_factory=list)
    #: Rules that match once the font stack is reduced to its first family — the
    #: transformation the importer performs because Builder's editor cannot parse
    #: a stack. Counted and reported, but not a defect, so deliberately NOT part
    #: of `ok`. In the *maintain* scenario, where nothing should be transformed,
    #: a non-zero count here is itself worth reading.
    font_stacks_reduced: int = 0

    @property
    def ok(self) -> bool:
        return not any(
            (self.missing_selectors, self.changed_selectors, self.missing_assets,
             self.unresolvable_assets, self.missing_text, self.missing_links,
             self.missing_scripts)
        )


def compare(source: str, clone: str, *, clone_origin: str = "") -> Comparison:
    src, cln = fetch(source), fetch(clone)
    result = Comparison()

    src_rules, cln_rules = _rules(src), _rules(cln)

    # Generated names are compared as bundles of declarations; everything else
    # is compared by name, because a name we control is a name that must match.
    def partition(rules):
        stable, generated = {}, []
        for selector, declarations in rules.items():
            head = selector.split()[-1] if " " in selector else selector
            if GENERATED_CLASS.match(head):
                generated.append(frozenset(declarations))
            else:
                stable[selector] = declarations
        return stable, generated

    src_stable, src_generated = partition(src_rules)
    cln_stable, cln_generated = partition(cln_rules)

    for selector, declarations in sorted(src_stable.items()):
        if selector not in cln_stable:
            result.missing_selectors.append(selector)
            continue
        lost = sorted(declarations - cln_stable[selector])
        if lost:
            result.changed_selectors[selector] = lost

    remaining = list(cln_generated)
    reduced_index: dict[frozenset[str], int] = {}
    for bundle in remaining:
        key = _reduce_font_stacks(bundle)
        reduced_index[key] = reduced_index.get(key, 0) + 1

    for bundle in src_generated:
        if bundle in remaining:
            remaining.remove(bundle)
            continue
        # Not an exact match. Before calling it missing, ask whether the only
        # difference is the font-stack reduction the importer performs on
        # purpose. If so it is a transformation, not an omission — but it is
        # still counted and reported, never silently dropped.
        key = _reduce_font_stacks(bundle)
        if reduced_index.get(key):
            reduced_index[key] -= 1
            result.font_stacks_reduced += 1
            continue
        # No rule in the clone says the same thing. Report the declarations,
        # since the name is meaningless on the other side.
        result.missing_selectors.append(
            "(generated rule) " + "; ".join(sorted(bundle))[:160]
        )

    src_assets, cln_assets = _assets(src), _assets(cln)
    result.missing_assets = sorted(a for a in src_assets if a not in cln_assets)

    # Present is not the same as working: a reference that 404s is an omission
    # the markup hides.
    origin = clone_origin or re.match(r"^https?://[^/]+", clone).group(0)
    for asset in sorted(cln_assets):
        if not asset.startswith("/"):
            continue
        code = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "10",
             f"{origin}{asset}"], capture_output=True, text=True,
        ).stdout
        if code != "200":
            result.unresolvable_assets.append(f"{asset} -> HTTP {code}")

    result.missing_text = sorted(t for t in _text(src) - _text(cln) if len(t) > 2)
    result.missing_links = sorted(_links(src) - _links(cln))
    result.missing_scripts = sorted(
        s[:120] for s in _scripts(src) - _scripts(cln)
    )
    return result


def report(result: Comparison, source: str, clone: str) -> None:
    print(f"clone-diff\n  source: {source}\n  clone : {clone}\n")

    sections = [
        ("CSS selectors present in the source and absent from the clone",
         result.missing_selectors),
        ("assets referenced by the source and not by the clone", result.missing_assets),
        ("assets the clone references but cannot serve", result.unresolvable_assets),
        ("text present in the source and absent from the clone", result.missing_text),
        ("links present in the source and absent from the clone", result.missing_links),
        ("scripts present in the source and absent from the clone", result.missing_scripts),
    ]
    for title, items in sections:
        if items:
            print(f"{title} — {len(items)}")
            for item in items[:15]:
                print(f"  {item}")
            if len(items) > 15:
                print(f"  ... and {len(items) - 15} more")
            print()

    if result.font_stacks_reduced:
        print(
            f"rules matching only after font-stack reduction — {result.font_stacks_reduced}\n"
            "  Not omissions. Builder stores font-family as a single family name, so\n"
            "  importing a site that used a stack necessarily drops the fallback chain.\n"
            "  Expected when IMPORTING a site. When MAINTAINING one, nothing should be\n"
            "  transformed, so a non-zero count here is worth reading.\n"
        )

    if result.changed_selectors:
        total = sum(len(v) for v in result.changed_selectors.values())
        print(f"declarations lost inside selectors that do exist — {total}")
        for selector, lost in list(result.changed_selectors.items())[:10]:
            print(f"  {selector}")
            for declaration in lost[:4]:
                print(f"      missing: {declaration}")
        print()

    if result.ok:
        print("No differences. Every selector, declaration, asset, link, text run")
        print("and script in the source is present in the clone.")
    else:
        print("The clone is NOT a faithful copy. Each line above is something the")
        print("source has and the clone does not — counts would have hidden all of it.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--source", required=True)
    parser.add_argument("--clone", required=True)
    args = parser.parse_args(argv)

    result = compare(args.source, args.clone)
    report(result, args.source, args.clone)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
