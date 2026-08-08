"""Transform zero — capture the reference every other transform answers to.

One run produces, under `sites/<site>/opt/baseline/`:

    crawl/*.html        served HTML per published route
    shots/*.png         route x viewport screenshots (deterministic ritual)
    features.json       extracted from the crawl, never hand-written
    state/              record checkpoint (capture_dev layout + manifest)
    scripts-scan.json   what each client script touches (see scan_scripts)
    manifest.json       routes, viewports, pins, checkpoint hash

The scan lives *here* because collapse and componentize are the transforms
that re-mint the classes scripts select against — they consult it to refuse a
merge that would break a script (ADR-009).

Everything is re-runnable: after a transform is accepted, a fresh baseline
becomes the new reference, so each step diffs against the last good state.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import shutil
import urllib.error
import urllib.request
from pathlib import Path

from buildsmith.workflows.optimize import shots as shots_mod

ROOT = Path(__file__).resolve().parents[3]

# -- script-dependency scan --------------------------------------------------

_JS_PATTERNS = {
    "ids": re.compile(r"getElementById\(\s*['\"]([^'\"]+)['\"]"),
    "selectors": re.compile(r"querySelector(?:All)?\(\s*['\"]([^'\"]+)['\"]"),
    "events": re.compile(r"addEventListener\(\s*['\"]([^'\"]+)['\"]"),
    "class_ops": re.compile(r"classList\.\w+\(\s*['\"]([^'\"]+)['\"]"),
}
_MINTED_CLASS = re.compile(r"\.((?:fb|bldr)-[0-9a-f]{4,})")
_CSS_SELECTOR_CLASS = re.compile(r"\.([A-Za-z_][\w-]*)")
_CSS_SELECTOR_ID = re.compile(r"#([A-Za-z_][\w-]*)")


def scan_script(name: str, script_type: str, body: str) -> dict:
    """What one client script depends on in the DOM.

    JavaScript: ids, selectors, classList operands, events bound.
    CSS: every class and id its selectors mention (declarations don't matter —
    a selector that stops matching is how a stylesheet silently dies).
    Both: minted `fb-*`/`bldr-*` classes get their own list, because those are
    exactly what collapse/componentize re-mint.
    """
    body = body or ""
    found: dict[str, list[str]] = {}
    if (script_type or "").lower() == "javascript":
        for key, pattern in _JS_PATTERNS.items():
            hits = sorted(set(pattern.findall(body)))
            if hits:
                found[key] = hits
    else:  # CSS — strip block comments, then read selectors before each '{'
        css = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
        selector_text = " ".join(
            part.rsplit("}", 1)[-1] for part in css.split("{")[:-1])
        classes = sorted(set(_CSS_SELECTOR_CLASS.findall(selector_text)))
        ids = sorted(set(_CSS_SELECTOR_ID.findall(selector_text)))
        if classes:
            found["classes"] = classes
        if ids:
            found["ids"] = ids
    minted = sorted(set(_MINTED_CLASS.findall(body)))
    if minted:
        found["minted_classes"] = minted
    return {"name": name, "script_type": script_type, "touches": found}


def collect_script_records(site_root: Path) -> tuple[list[dict], str]:
    """Every client-script source this site has, with a label saying which.

    Two sources, matching the two ways a site arrives (ADR-008): an adopted
    site's live export carries Builder Client Script records; an imported
    clone carries its page scripts as head JS assets (`assets/*-head-*.js`,
    TRAP-018 — Builder would Jinja-refuse them inline). An empty result means
    the caller must say UNSCANNED out loud, never ship an empty scan.
    """
    records: list[dict] = []
    source = ""
    export = site_root / "live-export" / "doctypes" / "builder-client-script.json"
    if export.exists():
        records += json.loads(export.read_text())
        source = "live-export"
    assets = site_root / "assets"
    head_js = sorted(assets.glob("*-head-*.js")) if assets.is_dir() else []
    if head_js:
        records += [
            {"name": f.name, "script_type": "JavaScript",
             "script": f.read_text(encoding="utf-8", errors="replace")}
            for f in head_js
        ]
        source = (source + " + " if source else "") + \
            f"assets/*-head-*.js ({len(head_js)})"
    return records, source


def scan_scripts(records: list[dict]) -> dict:
    """Scan every Builder Client Script record; see scan_script."""
    scans = [scan_script(r.get("name", "?"), r.get("script_type", ""),
                         r.get("script", "")) for r in records]
    minted = sorted({c for s in scans
                     for c in s["touches"].get("minted_classes", [])})
    return {
        "_meta": {
            "scripts": len(scans),
            "note": "consulted by collapse/componentize before re-minting "
                    "classes; a transform breaking a scanned dependency is "
                    "refused or flagged (ADR-009)",
        },
        "minted_classes_all": minted,
        "scripts": scans,
    }


# -- capture ------------------------------------------------------------------

class CannotCapture(Exception):
    """Baseline could not be captured — callers exit 2, never 0."""


def _fetch(url: str, *, timeout: float, opener=None) -> tuple[int, str]:
    request = urllib.request.Request(
        url, headers={"User-Agent": "buildsmith-optimize/0.1 (baseline)"})
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except (urllib.error.URLError, OSError) as exc:
        # "could not check", not "found a problem" — SystemExit here would be
        # reported as exit 1 by the CLI's generic handler.
        raise CannotCapture(f"cannot reach {url}: {exc}") from exc


def build_baseline(site: str, *, clone_url: str = "http://127.0.0.1:8000",
                   target: str = "sandbox.localhost",
                   out: Path | None = None, timeout: float = 15.0,
                   force: bool = False,
                   opener=None, shooter=None, capturer=None) -> dict:
    """Capture the full pre-transform reference. Returns the manifest.

    Refuses while the gate ledger holds an applied transform with no passing
    oracle — re-baselining then would absorb the unproven change into the
    reference forever. `force=True` waives (and records the waiver).

    `opener`/`shooter`/`capturer` are injection points for tests; the real
    ones are urllib, shots.capture_shots and capture_dev.capture.
    """
    from buildsmith.tools import capture_dev, sandbox
    from buildsmith.workflows.optimize import gates
    from buildsmith.workflows.replicate import extract_site

    waived = gates.assert_no_pending(site, force=force)
    if waived:
        names = ", ".join(sorted({e["transform"] for e in waived}))
        print(f"WAIVED by --force, recorded in the ledger: {names} "
              "(applied without a passing oracle)")

    out = out or (ROOT / "sites" / site / "opt" / "baseline")
    crawl_dir = out / "crawl"
    if crawl_dir.exists():          # wipe whole tree: nested routes make
        shutil.rmtree(crawl_dir)    # subdirectories a flat unlink misses
    crawl_dir.mkdir(parents=True)

    # 1. record checkpoint first — the routes come from the records, so the
    #    crawl can never quietly miss an unlinked page (the reason W1's BFS
    #    is not reused here).
    capture = capturer or (lambda: capture_dev.capture(
        site, target=target, out=out / "state"))
    checkpoint = capture()

    # 2. served HTML per route; unpublished/unreachable routes are recorded,
    #    never silently dropped.
    captured: list[str] = []
    skipped: dict[str, int] = {}
    base = clone_url.rstrip("/")
    for route in checkpoint["routes"]:
        status, html = _fetch(f"{base}/{route.lstrip('/')}".rstrip("/") or base,
                              timeout=timeout, opener=opener)
        if status == 200 and html:
            # nested routes flatten to subdirectories, matching W1's layout;
            # extract_site reads only the top level, so nested routes would
            # miss feature extraction — flat-route sites (this one) are whole.
            path = crawl_dir / f"{route or 'index'}.html"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(html)
            captured.append(route)
        else:
            skipped[route] = status

    # 3. features — extracted, never hand-written (AGENTS.md). Guard the
    #    coverage explicitly: a captured route missing from the inventory is
    #    a silent hole in the checklist, not a smaller checklist.
    inventory = extract_site(crawl_dir, site=site)
    uncovered = [r for r in captured if r not in inventory.routes]
    if uncovered:
        raise CannotCapture(
            f"features.json covers no entry for captured route(s) "
            f"{uncovered} — extraction and crawl disagree")
    inventory.write(out / "features.json")

    # 4. deterministic screenshots. Wipe first — a stale shot for a removed
    #    route would otherwise keep "passing" against nothing.
    shots_dir = out / "shots"
    if shots_dir.exists():
        shutil.rmtree(shots_dir)
    shoot = shooter or shots_mod.capture_shots
    written = shoot(clone_url, captured, shots_dir)

    # 5. script-dependency scan. Two sources, matching the two ways a site
    #    arrives (ADR-008): an adopted site's live export carries Builder
    #    Client Script records; an imported clone carries its page scripts as
    #    head JS assets (`assets/*-head-*.js`, TRAP-018 — Builder would
    #    Jinja-refuse them inline). When neither exists the manifest says so
    #    out loud rather than shipping an empty scan that reads as "no
    #    scripts".
    script_records, scan_source = collect_script_records(ROOT / "sites" / site)
    if script_records:
        scan = scan_scripts(script_records)
        (out / "scripts-scan.json").write_text(json.dumps(scan, indent=2) + "\n")
        scripts_scanned: int | str = scan["_meta"]["scripts"]
    else:
        scripts_scanned = "UNSCANNED — no client-script source found"

    manifest = {
        "site": site,
        "clone_url": clone_url,
        "created_utc": _dt.datetime.now(_dt.UTC).isoformat(
            timespec="seconds"),
        "builder_ref": sandbox.load_pins().get("BUILDER_REF", "?"),
        "routes_captured": captured,
        "routes_skipped": skipped,
        "viewports": [list(v) for v in shots_mod.VIEWPORTS],
        "shots": sorted(written),
        "checkpoint": {"content_hash": checkpoint["content_hash"],
                       "counts": checkpoint["counts"]},
        "scripts_scanned": scripts_scanned,
        "scripts_scan_source": scan_source,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest
