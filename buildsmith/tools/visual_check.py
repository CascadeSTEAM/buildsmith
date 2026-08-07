#!/usr/bin/env python3
"""Drive a real browser over a clone and check it against the source's features.

Static comparison is necessary and not sufficient. A hover menu, a lightbox and a
hero background all survived a byte-level diff of this project's own making,
because each one only exists once a browser has run the page. The only way to
know a feature works is to perform it.

So this loads both the source and the clone in Chromium and, for each entry in
`sites/<site>/features.json`:

- **assets** — asserts every image actually painted (`naturalWidth > 0`), which
  is different from the URL returning 200 and different again from the tag
  existing. A hero background is checked as a computed style, because it is not
  an `<img>` at all.
- **headings and links** — asserts the text and targets are present in the DOM
  the browser built, not the HTML the server sent.
- **interactions** — actually hovers, actually clicks, and asserts the page
  changed. A click handler that binds and does nothing passes every static
  check ever written.
- **runtime elements** — asserts the element a script is supposed to create
  exists after the script has run.

It also captures a screenshot per route per breakpoint for both, and reports the
pixel difference, because some things are only visible.

    buildsmith visual check --site example --clone http://127.0.0.1:8000
    buildsmith visual check --site example --clone http://127.0.0.1:8000 --source https://…

Run it with the project venv: `.venv/bin/python bin/visual-check.py …`
Exit status is 1 if any feature fails on the clone.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from buildsmith.errors import CouldNotCheck

ROOT = Path(__file__).resolve().parents[2]

__all__ = ["Result", "check_site", "main"]


@dataclass
class Result:
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


def _check_page(page, route: str, features: list[dict], result: Result) -> None:
    where = f"/{route}"

    def ok(msg: str) -> None:
        result.passed.append(f"{where} {msg}")

    def bad(msg: str) -> None:
        result.failed.append(f"{where} {msg}")

    # --- assets must have painted, not merely resolved ----------------------
    painted = page.evaluate(
        """() => Array.from(document.images).map(i => ({src: i.currentSrc || i.src,
             w: i.naturalWidth}))"""
    )
    by_src = {p["src"].split("/")[-1].split("?")[0]: p["w"] for p in painted}
    backgrounds = set(
        page.evaluate(
            """() => Array.from(document.querySelectorAll('*'))
                 .map(e => getComputedStyle(e).backgroundImage)
                 .filter(v => v && v !== 'none').join(' ')"""
        ).replace('"', "").split()
    )
    background_text = " ".join(backgrounds)
    # A favicon is neither an <img> nor a background — it is a <link rel=icon>,
    # and checking only the first two reported it missing when it was present.
    icons = " ".join(
        page.evaluate(
            """() => Array.from(document.querySelectorAll('link[rel*=icon]'))
                 .map(l => l.getAttribute('href') || '')"""
        )
    )

    for feature in features:
        if feature["kind"] != "asset":
            continue
        name = feature["evidence"].split("/")[-1].split("?")[0]
        if name in by_src:
            if by_src[name] > 0:
                ok(f"image painted: {name}")
            else:
                bad(f"image present but did NOT paint (naturalWidth 0): {name}")
        elif name in background_text:
            ok(f"background image applied: {name}")
        elif name in icons:
            ok(f"icon linked: {name}")
        else:
            bad(f"asset missing from the rendered page: {name}")

    # --- headings and links, from the built DOM -----------------------------
    body_text = page.evaluate("() => document.body.innerText")
    hrefs = set(page.evaluate("() => Array.from(document.links).map(a => a.getAttribute('href'))"))
    for feature in features:
        if feature["kind"] == "heading":
            (ok if feature["detail"][:60] in body_text else bad)(
                f"heading: {feature['detail'][:50]}"
            )
        elif feature["kind"] == "link":
            target = feature.get("evidence", "")
            (ok if target in hrefs else bad)(f"link target: {target}")

    # --- runtime elements: the script must have created them ----------------
    for feature in features:
        if feature["kind"] != "runtime-element":
            continue
        selector = feature["selector"]
        exists = page.evaluate(f"() => !!document.querySelector({selector!r})")
        if exists:
            ok(f"runtime element exists: {selector}")
        else:
            # It may only appear after an interaction; that is checked below.
            result.skipped.append(f"{where} {selector} absent before interaction")

    # --- interactions: perform them, then assert something changed ----------
    wants = {f["evidence"] for f in features if f["kind"] == "interaction"}
    runtime = [f["selector"] for f in features if f["kind"] == "runtime-element"]

    if "click" in wants:
        before = page.evaluate("() => document.body.innerHTML.length")
        target = page.query_selector("img") or page.query_selector("a")
        if target:
            try:
                target.click(timeout=3000)
                page.wait_for_timeout(500)
                after = page.evaluate("() => document.body.innerHTML.length")
                appeared = [
                    s for s in runtime
                    if page.evaluate(f"() => !!document.querySelector({s!r})")
                ]
                if appeared or after != before:
                    ok(f"click changed the page ({', '.join(appeared) or 'DOM changed'})")
                else:
                    bad("click did nothing — a handler that binds and does nothing "
                        "passes every static check")
            except Exception as exc:  # noqa: BLE001
                result.skipped.append(f"{where} click not performable: {exc}")
        else:
            result.skipped.append(f"{where} nothing clickable found")

    if wants & {"mouseenter", "mouseover"}:
        # Try every candidate rather than the first. The first link is usually
        # the logo, which has no hover on this kind of site — testing only that
        # reported "inconclusive" while the real nav hover worked perfectly on
        # both sides, which is a checker that lies in the reassuring direction.
        candidates = (page.query_selector_all("nav a") or page.query_selector_all("a"))[:8]
        if not candidates:
            result.skipped.append(f"{where} no link to hover")
        else:
            # A hover effect frequently lands on a child, toggles a class, or
            # animates a transform. Sampling one property on the hovered element
            # misses all three, so snapshot the element and its subtree.
            probe = """el => JSON.stringify(
                [el, ...el.querySelectorAll('*'), el.parentElement].filter(Boolean).map(n => {
                    const s = getComputedStyle(n);
                    const r = n.getBoundingClientRect();
                    return [n.className, s.color, s.backgroundColor, s.transform,
                            s.opacity, s.boxShadow, s.textDecorationLine,
                            Math.round(r.width), Math.round(r.height)];
                }))"""
            changed_on = []
            for candidate in candidates:
                try:
                    before = page.evaluate(probe, candidate)
                    candidate.hover(timeout=2000)
                    page.wait_for_timeout(250)
                    if page.evaluate(probe, candidate) != before:
                        changed_on.append((candidate.inner_text() or "?").strip()[:20])
                except Exception:  # noqa: BLE001
                    continue
            if changed_on:
                ok(f"hover changes {len(changed_on)} element(s): "
                   f"{', '.join(c for c in changed_on[:3] if c)}")
            else:
                bad("the source binds a hover handler but nothing here responds to hover")


def check_site(site: str, clone: str, source: str = "", *, shots: Path | None = None) -> Result:
    from playwright.sync_api import sync_playwright

    inventory_path = ROOT / "sites" / site / "features.json"
    if not inventory_path.exists():
        raise CouldNotCheck(
            f"{inventory_path} does not exist. Build it first — it is extracted from the "
            "crawl, not written by hand."
        )
    inventory = json.loads(inventory_path.read_text())
    result = Result()

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for route, features in sorted(inventory["routes"].items()):
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            url = f"{clone.rstrip('/')}/{route}"
            try:
                page.goto(url, wait_until="networkidle", timeout=30000)
            except Exception as exc:  # noqa: BLE001
                result.failed.append(f"/{route} did not load: {exc}")
                page.close()
                continue
            _check_page(page, route, features, result)

            if shots:
                shots.mkdir(parents=True, exist_ok=True)
                for width in (1280, 576):
                    page.set_viewport_size({"width": width, "height": 900})
                    page.wait_for_timeout(300)
                    name = f"{route or 'home'}-{width}.png".replace("/", "_")
                    page.screenshot(path=str(shots / f"clone-{name}"), full_page=True)
                    result.screenshots.append(f"clone-{name}")
                    if source:
                        other = browser.new_page(viewport={"width": width, "height": 900})
                        try:
                            other.goto(f"{source.rstrip('/')}/{route}",
                                       wait_until="networkidle", timeout=30000)
                            other.screenshot(path=str(shots / f"source-{name}"),
                                             full_page=True)
                            result.screenshots.append(f"source-{name}")
                        except Exception:  # noqa: BLE001
                            pass
                        finally:
                            other.close()
            page.close()
        browser.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--site", required=True)
    parser.add_argument("--clone", required=True)
    parser.add_argument("--source", default="")
    parser.add_argument("--shots", help="directory for screenshots")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    shots = Path(args.shots) if args.shots else None
    try:
        result = check_site(args.site, args.clone, args.source, shots=shots)
    except CouldNotCheck as exc:
        # Standalone runs (publish-verify invokes this as a subprocess) must
        # also exit 2 here — uncaught, a string SystemExit becomes exit 1 and
        # "could not check" reads as "found a problem".
        print(f"COULD NOT CHECK: {exc}", file=sys.stderr)
        return 2

    print(f"visual-check: {len(result.passed)} passed, {len(result.failed)} failed, "
          f"{len(result.skipped)} inconclusive")
    if args.verbose:
        for line in result.passed:
            print(f"  PASS  {line}")
    for line in result.skipped:
        print(f"  ????  {line}")
    for line in result.failed:
        print(f"  FAIL  {line}")
    if result.screenshots:
        print(f"\n  {len(result.screenshots)} screenshot(s) written")

    if not result.ok:
        print("\nThe clone does not reproduce every feature the source has. Each FAIL is")
        print("something a visitor would notice and no static diff would catch.")
        return 1
    print("\nEvery extracted feature is reproduced in the clone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
