"""The rendering oracle — "changed nothing visible", proven or refused.

Re-shoots the site with the same deterministic ritual as the baseline and
pixel-diffs every route x viewport pair. Exit semantics follow the repo rule:

    0  every pair within threshold        — proved
    1  any pair over threshold, missing,  — found a problem
       or structurally different
    2  could not check                    — no baseline, playwright absent,
                                            or a PNG outside the codec subset

A failed pair writes a diff artifact (changed pixels in red) next to the
report, because "0.4% of pixels differ" is unactionable without seeing which.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from buildsmith.errors import CouldNotCheck
from buildsmith.workflows.optimize import png
from buildsmith.workflows.optimize import shots as shots_mod

ROOT = Path(__file__).resolve().parents[3]

#: fraction of pixels allowed to differ per shot before it is a finding.
#: The settle ritual measures exactly 0.0000% on an unchanged site, so this
#: is headroom for OS-level rendering drift, not for real change — 0.1% was
#: tried first and let a 6-element colour swap slip through on a tall page,
#: because a global ratio dilutes with page height.
DEFAULT_THRESHOLD = 0.0001
#: per-channel delta treated as rendering noise rather than change
DEFAULT_TOLERANCE = 2


class CannotCheck(CouldNotCheck):
    """The oracle could not run at all — exit 2, never 0.

    A subclass of :class:`buildsmith.errors.CouldNotCheck`, so the CLI's one
    handler maps it; the local name stays because call sites read better."""


def run_oracle(site: str, *, clone_url: str | None = None,
               baseline_dir: Path | None = None,
               threshold: float = DEFAULT_THRESHOLD,
               tolerance: int = DEFAULT_TOLERANCE,
               shooter=None) -> dict:
    """Compare the site as served now against its stored baseline.

    Returns the report dict; `report["ok"]` is the verdict. Raises
    CannotCheck when there is nothing trustworthy to compare.
    """
    base_dir = baseline_dir or (ROOT / "sites" / site / "opt" / "baseline")
    manifest_path = base_dir / "manifest.json"
    if not manifest_path.exists():
        raise CannotCheck(
            f"no baseline at {base_dir} — run `buildsmith optimize baseline` first")
    manifest = json.loads(manifest_path.read_text())

    out = base_dir.parent / "oracle"
    if out.exists():
        shutil.rmtree(out)
    (out / "shots").mkdir(parents=True)

    url = clone_url or manifest["clone_url"]
    viewports = tuple(tuple(v) for v in manifest["viewports"])
    shoot = shooter or shots_mod.capture_shots
    try:
        shoot(url, manifest["routes_captured"], out / "shots",
              viewports=viewports)
    except shots_mod.PlaywrightMissing as exc:
        raise CannotCheck(f"playwright missing: {exc}") from exc

    pairs = []
    failed = 0
    for name in manifest["shots"]:
        before_path = base_dir / "shots" / name
        after_path = out / "shots" / name
        entry: dict = {"shot": name}
        if not before_path.exists():
            raise CannotCheck(f"baseline shot vanished: {before_path}")
        if not after_path.exists():
            entry.update(ok=False, error="route no longer captured")
            failed += 1
            pairs.append(entry)
            continue
        try:
            before = png.decode(before_path.read_bytes())
            after = png.decode(after_path.read_bytes())
        except (png.UnsupportedPng, png.CorruptPng) as exc:
            raise CannotCheck(f"{name}: {exc}") from exc
        result = png.diff(before, after, tolerance=tolerance)
        entry.update(
            ok=result.within(threshold),
            differing=result.differing, total=result.total,
            ratio=round(result.ratio, 6),
        )
        if result.size_mismatch:
            entry["size_mismatch"] = result.size_mismatch
        if result.bbox:
            entry["bbox"] = list(result.bbox)
        if not entry["ok"]:
            failed += 1
            if not result.size_mismatch:
                artifact = png.diff_artifact(before, after,
                                             tolerance=tolerance)
                artifact_path = out / f"diff-{name}"
                artifact_path.write_bytes(png.encode(artifact))
                entry["artifact"] = str(artifact_path)
        pairs.append(entry)

    report = {
        "site": site,
        "clone_url": url,
        "baseline_created_utc": manifest["created_utc"],
        "threshold": threshold,
        "tolerance": tolerance,
        "ok": failed == 0,
        "failed": failed,
        "pairs": pairs,
    }
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def render_report(report: dict) -> str:
    lines = [f"rendering oracle — {report['site']} vs baseline "
             f"({report['baseline_created_utc']})",
             f"  threshold {report['threshold']:.4%} of pixels, "
             f"tolerance ±{report['tolerance']}/channel"]
    for pair in report["pairs"]:
        if "error" in pair:
            verdict = f"FAIL  {pair['error']}"
        elif pair.get("size_mismatch"):
            verdict = f"FAIL  size changed: {pair['size_mismatch']}"
        else:
            verdict = ("ok   " if pair["ok"] else "FAIL ") + \
                f" {pair['ratio']:.4%} differ"
            if not pair["ok"] and pair.get("bbox"):
                x0, y0, x1, y1 = pair["bbox"]
                verdict += f" in ({x0},{y0})..({x1},{y1})"
        lines.append(f"  {pair['shot']:32s} {verdict}")
    lines.append("PROVED: rendering unchanged." if report["ok"]
                 else f"PROBLEM: {report['failed']} shot(s) differ — "
                      "see the diff artifacts.")
    return "\n".join(lines)
