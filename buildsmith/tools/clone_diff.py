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

__all__ = ["Comparison", "compare", "compare_rules", "fetch", "main"]


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


def _reducible_stack(declaration: str, clone_bundle: frozenset[str]) -> bool:
    """Whether `declaration` is a font stack whose first family the clone carries.

    The importer reduces every stack to its first family
    (`_primary_font_family`), so `font-family: Nunito, ui-sans-serif, sans-serif`
    becoming `font-family: Nunito` is the intended transformation. Two safety
    rails hold the line against absorbing real defects:

    - it must be a real STACK (two or more families). A single family in the
      source and a different one in the clone is a defect — in the *maintain*
      scenario, where nothing should be transformed, that is exactly a font
      change — and stays one;
    - the clone must carry the FIRST family. Keeping the second family instead
      is a converter bug that differs from the source in the same shape as the
      intended reduction, and must not be waved through.
    """
    match = _FONT_DECL.match(declaration.strip())
    if not match:
        return False
    families = [f.strip().strip("'\"")
                for f in match.group(1).replace("\\ ", " ").split(",") if f.strip()]
    return len(families) > 1 and f"font-family: {families[0]}" in clone_bundle


def _generated_signature(context: str, missing: frozenset[str], extra: frozenset[str]):
    """The shape of a generated-rule difference, as a hashable grouping key.

    Rules whose only difference is the same set of declarations — the renderer
    adding `overflow-wrap: break-word` to every text rule, say — collapse into
    one report line instead of one line per hash. The media condition is part
    of the shape: `display: none` under `(max-width: 767px)` is not the same
    difference as under `(min-width: 768px)`.
    """
    return (context, tuple(sorted(missing)), tuple(sorted(extra)))


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
    #: Generated rules the source has that NO clone rule says — grouped by the
    #: signature of the difference (media context, missing declarations, then
    #: clone-added ones), so a renderer that adds one declaration to forty rules
    #: reads as one finding, not forty. A non-empty map fails `ok`, exactly like
    #: a missing stable selector: declarations the source has and the clone does
    #: not are an omission by any other name.
    generated_omitted: dict[tuple[str, tuple[str, ...], tuple[str, ...]], int] = (
        field(default_factory=dict)
    )
    #: Generated rules fully covered by a clone rule — the clone carries every
    #: source declaration and adds its own (`source ⊆ clone`). Reported, not a
    #: failure: nothing the source says is absent. The added declarations are
    #: still a difference worth seeing, so they are never silently dropped.
    generated_covered: dict[tuple[str, tuple[str, ...], tuple[str, ...]], int] = (
        field(default_factory=dict)
    )

    @property
    def generated_missing(self) -> int:
        """How many generated rules are genuinely missing source declarations."""
        return sum(self.generated_omitted.values())

    @property
    def ok(self) -> bool:
        return not any(
            (self.missing_selectors, self.changed_selectors, self.missing_assets,
             self.unresolvable_assets, self.missing_text, self.missing_links,
             self.missing_scripts, self.generated_omitted)
        )


def compare_rules(
    src_rules: dict[str, set[str]], cln_rules: dict[str, set[str]]
) -> Comparison:
    """Compare two parsed rule maps — the CSS core of `compare`.

    Called directly by `publish_verify`, which already has both pages' HTML
    (they share a port and differ by Host header, so fetching by URL would hit
    the wrong site). Keeping the verdict in one place matters: a second
    implementation of the same job is how one of them compared selector *names*
    and reported 71 missing rules on a page that was identical to dev.

    Generated names are compared as bundles of declarations; everything else is
    compared by name, because a name we control is a name that must match. The
    media condition a generated rule lives under is part of its identity — the
    same declarations under different breakpoints are different rules.
    """
    result = Comparison()

    def partition(rules):
        stable, generated = {}, []
        for selector, declarations in rules.items():
            head = selector.split()[-1] if " " in selector else selector
            if GENERATED_CLASS.match(head):
                context = ""
                if selector.startswith("@media"):
                    context = selector[: selector.rfind(head)].rstrip()
                generated.append((context, frozenset(declarations)))
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
    reduced_index: dict[tuple[str, frozenset[str]], int] = {}
    for context, bundle in remaining:
        key = (context, _reduce_font_stacks(bundle))
        reduced_index[key] = reduced_index.get(key, 0) + 1

    for context, bundle in src_generated:
        if (context, bundle) in remaining:
            remaining.remove((context, bundle))
            continue
        # Not an exact match. Before calling it missing, ask whether the only
        # difference is the font-stack reduction the importer performs on
        # purpose. If so it is a transformation, not an omission — but it is
        # still counted and reported, never silently dropped.
        key = (context, _reduce_font_stacks(bundle))
        if reduced_index.get(key):
            reduced_index[key] -= 1
            result.font_stacks_reduced += 1
            continue
        # Judge the difference against the clone rule it most resembles, never
        # against "nothing at all": the two renderers may differ — the live
        # Builder emits font stacks where the pin emits single families and
        # `overflow-wrap` — and that shape of difference is the importer's own
        # transformation plus a renderer artifact, not forty omissions. Only
        # rules under the same media condition are candidates.
        pool = [c for c in remaining if c[0] == context]
        if not pool:
            key = _generated_signature(context, bundle, frozenset())
            result.generated_omitted[key] = result.generated_omitted.get(key, 0) + 1
            continue
        closest = max(pool, key=lambda c: len(c[1] & bundle))
        missing = bundle - closest[1]
        extra = closest[1] - bundle
        if not missing:
            # The source rule says nothing the clone rule does not: the clone
            # covers it and adds declarations. Reported, not an omission.
            key = _generated_signature(context, missing, extra)
            result.generated_covered[key] = result.generated_covered.get(key, 0) + 1
            continue
        if all(_reducible_stack(declaration, closest[1]) for declaration in missing):
            # The only absent declarations are font stacks the importer reduces
            # to a first family the clone carries. Same transformation as the
            # exact reduced match above, plus declarations the clone's renderer
            # adds — which are not something the source had.
            result.font_stacks_reduced += 1
            continue
        # Genuine omission: declarations the source has that no clone rule has.
        key = _generated_signature(context, missing, extra)
        result.generated_omitted[key] = result.generated_omitted.get(key, 0) + 1

    return result


def compare(source: str, clone: str, *, clone_origin: str = "") -> Comparison:
    src, cln = fetch(source), fetch(clone)
    result = compare_rules(_rules(src), _rules(cln))

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

    if result.generated_missing:
        print(
            f"generated rules with source declarations no clone rule carries — "
            f"{result.generated_missing} ({len(result.generated_omitted)} distinct "
            "difference(s))\n"
        )
        for (context, missing, _extra), count in sorted(
            result.generated_omitted.items(), key=lambda kv: -kv[1]
        ):
            print(f"  {count}× rules differ by:")
            if context:
                print(f"      under {context}")
            for declaration in missing:
                print(f"      missing: {declaration}")
        print()

    if result.font_stacks_reduced:
        print(
            f"rules matching only after font-stack reduction — {result.font_stacks_reduced}\n"
            "  Not omissions. Builder stores font-family as a single family name, so\n"
            "  importing a site that used a stack necessarily drops the fallback chain.\n"
            "  Expected when IMPORTING a site. When MAINTAINING one, nothing should be\n"
            "  transformed, so a non-zero count here is worth reading.\n"
        )

    if result.generated_covered:
        total = sum(result.generated_covered.values())
        print(
            f"generated rules the clone fully covers by adding declarations — {total} "
            f"({len(result.generated_covered)} distinct)\n"
            "  Not omissions: every source declaration is present inside a clone rule,\n"
            "  which carries extra ones of its own (e.g. a renderer adding overflow-wrap).\n"
        )
        for (context, _missing, extra), count in sorted(
            result.generated_covered.items(), key=lambda kv: -kv[1]
        ):
            print(f"  {count}× clone rules add:")
            if context:
                print(f"      under {context}")
            for declaration in extra:
                print(f"      added: {declaration}")
        print()

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
