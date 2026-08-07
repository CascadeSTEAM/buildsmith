"""Deterministic screenshots — the oracle is only as good as its negatives.

A naive screenshot pass is flaky: animations mid-frame, fonts swapping in
late, lazy images that never loaded. Every capture here goes through the same
settle ritual so that two shots of an *unchanged* page are pixel-stable, which
is the property the whole Phase A gate rests on (ADR-009).

Playwright is the one optional dependency, exactly as in `visual_check`;
callers turn `PlaywrightMissing` into exit 2.
"""
from __future__ import annotations

from pathlib import Path

#: width x height. 1280/576 match visual_check's long-standing pair; 768 adds
#: the tablet band Builder's own breakpoints care about.
VIEWPORTS: tuple[tuple[int, int], ...] = ((1280, 900), (768, 900), (576, 900))

_FREEZE_CSS = """
*, *::before, *::after {
  animation: none !important;
  transition: none !important;
  caret-color: transparent !important;
}
html { scroll-behavior: auto !important; }
"""


class PlaywrightMissing(Exception):
    """playwright is not installed — capture cannot run (exit 2, never 0)."""


def shot_name(route: str, width: int) -> str:
    """Stable, filesystem-safe name for one route x viewport pair."""
    slug = route.strip("/").replace("/", "--") or "home"
    return f"{slug}-{width}.png"


def capture_shots(base_url: str, routes: list[str], out_dir: str | Path,
                  *, viewports: tuple[tuple[int, int], ...] = VIEWPORTS,
                  settle_ms: int = 250) -> dict[str, str]:
    """Screenshot every route at every viewport. Returns {shot name: path}.

    The settle ritual, per page load: reduced-motion emulation, animation
    freeze CSS, `document.fonts.ready`, a full lazy-load scroll pass, then a
    short fixed settle before each shot.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:                       # pragma: no cover
        raise PlaywrightMissing(str(exc)) from exc

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    base = base_url.rstrip("/")
    written: dict[str, str] = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for width, height in viewports:
                context = browser.new_context(
                    viewport={"width": width, "height": height},
                    device_scale_factor=1,
                    reduced_motion="reduce",
                )
                page = context.new_page()
                for route in routes:
                    url = f"{base}/{route.lstrip('/')}".rstrip("/") or base
                    page.goto(url, wait_until="networkidle")
                    page.add_style_tag(content=_FREEZE_CSS)
                    page.evaluate("document.fonts.ready")
                    # walk the page so lazy loaders fire, then return to top
                    page.evaluate(
                        """async () => {
                             const step = window.innerHeight;
                             for (let y = 0; y < document.body.scrollHeight;
                                  y += step) {
                               window.scrollTo(0, y);
                               await new Promise(r => setTimeout(r, 40));
                             }
                             window.scrollTo(0, 0);
                           }""")
                    page.wait_for_timeout(settle_ms)
                    name = shot_name(route, width)
                    path = out / name
                    page.screenshot(path=str(path), full_page=True)
                    written[name] = str(path)
                context.close()
        finally:
            browser.close()
    return written
