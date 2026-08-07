"""`optimize status` — where am I in the pipeline, answered from artifacts.

Before this existed the answer lived in the operator's memory and the
journal's prose (bootstrap critical review §5): the baseline manifest, the gate ledger, and
three proposal files each knew their own fragment, and nothing composed
them. This reads exactly those artifacts and executes nothing — no sandbox,
no HTTP, no bench. A status command that mutates state is not a status
command.

What it can and cannot say:

- It CAN say whether a baseline exists, which transforms were applied
  against it, which of those the oracle proved, and what each proposal file
  is waiting on.
- It CANNOT say whether the sandbox content still matches the checkpoint —
  that comparison needs a capture, and belongs to the transforms' own
  staleness guard. Absence of a claim here is deliberate, not an oversight.
"""

from __future__ import annotations

import json
from pathlib import Path

from buildsmith.errors import CouldNotCheck
from buildsmith.workflows.optimize import gates

ROOT = Path(__file__).resolve().parents[3]

#: Phase A transform order per ADR-009 — collapse normalises shapes before
#: componentize mines them. Used for display order only: enforcement of the
#: order stays with the transforms themselves.
PIPELINE = ("tokenize", "fonts", "collapse", "componentize")

#: Proposal file per transform, relative to sites/<site>/opt/proposals/.
#: collapse keeps decisions in its run log, not a proposal file.
_PROPOSAL_FILES = {
    "tokenize": "tokens.json",
    "fonts": "fonts.json",
    "componentize": "components.json",
}

__all__ = ["PIPELINE", "gather", "render"]


def _proposal_summary(path: Path) -> dict | None:
    """Counts by status, or None when the transform was never mined."""
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    by_status: dict[str, int] = {}
    for proposal in data.get("proposals", []):
        status = proposal.get("status", "proposed")
        by_status[status] = by_status.get(status, 0) + 1
    return {
        "file": str(path.relative_to(ROOT)),
        "by_status": by_status,
        "total": sum(by_status.values()),
        "orphaned": len(data.get("orphaned", [])),
    }


def gather(site: str) -> dict:
    """Everything the artifacts can prove about the pipeline, JSON-able."""
    site_dir = ROOT / "sites" / site
    if not site_dir.is_dir():
        raise CouldNotCheck(
            f"no site directory at sites/{site} — nothing to report on. "
            "A status over a mistyped site name must not read as 'not started'."
        )

    opt = site_dir / "opt"

    baseline: dict | None = None
    # The baseline's own manifest — NOT state/manifest.json, which is the
    # record checkpoint's (capture_dev layout, content_hash at top level,
    # what gates.baseline_hash reads). Two files, two shapes, same name.
    manifest_path = opt / "baseline" / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        baseline = {
            "created_utc": manifest.get("created_utc"),
            "content_hash": (manifest.get("checkpoint") or {}).get("content_hash"),
            "builder_ref": manifest.get("builder_ref"),
            "routes_captured": len(manifest.get("routes_captured") or []),
            "routes_skipped": len(manifest.get("routes_skipped") or {}),
            "scripts_scanned": manifest.get("scripts_scanned"),
        }

    entries = gates._load(site)["entries"]
    ledger = {
        "entries": entries,
        "applied": len(entries),
        "proved": sum(1 for e in entries if (e.get("oracle") or {}).get("ok")),
        "failed": sum(
            1 for e in entries
            if e.get("oracle") is not None and not (e.get("oracle") or {}).get("ok")
        ),
        "waived": sum(1 for e in entries if e.get("waived")),
        "pending": gates.pending(site),
    }

    proposals = {
        transform: _proposal_summary(opt / "proposals" / filename)
        for transform, filename in _PROPOSAL_FILES.items()
    }

    # Presence of run artifacts, NOT an "applied" claim: collapse writes its
    # dir on dry runs too, and applies that predate the gate ledger left
    # artifacts with no entry. The render step surfaces that gap explicitly
    # rather than letting "0 applied" read as "nothing ever ran".
    artifacts = {
        transform: any((opt / "transforms" / transform).glob("*"))
        for transform in PIPELINE
    }

    return {"site": site, "baseline": baseline, "gates": ledger,
            "proposals": proposals, "artifacts": artifacts}


def render(data: dict) -> str:
    """The human view. One screen, worst news first."""
    lines = [f"W3 optimize — site {data['site']}"]

    pending = data["gates"]["pending"]
    if pending:
        lines.append("")
        names = ", ".join(sorted({e["transform"] for e in pending}))
        lines.append(
            f"!! {len(pending)} applied transform(s) with no passing oracle: "
            f"{names}"
        )
        lines.append("   the sandbox holds unproven changes — run "
                     "`buildsmith optimize oracle`")

    lines.append("")
    baseline = data["baseline"]
    if baseline is None:
        lines.append("baseline   NONE — every transform refuses without one. "
                     "First step: `buildsmith optimize baseline`")
    else:
        content_hash = (baseline["content_hash"] or "?")[:12]
        lines.append(
            f"baseline   {baseline['created_utc']}  checkpoint {content_hash}  "
            f"{baseline['routes_captured']} route(s)"
            + (f" ({baseline['routes_skipped']} skipped)"
               if baseline["routes_skipped"] else "")
            + f"  builder {baseline['builder_ref']}"
        )
        if isinstance(baseline["scripts_scanned"], str):
            # the manifest's own loud UNSCANNED marker — pass it through
            lines.append(f"           scripts: {baseline['scripts_scanned']}")

    ledger = data["gates"]
    lines.append(
        f"gates      {ledger['applied']} applied · {ledger['proved']} proved"
        + (f" · {ledger['failed']} failed-oracle" if ledger["failed"] else "")
        + (f" · {ledger['waived']} waived" if ledger["waived"] else "")
        + (f" · {len(ledger['pending'])} PENDING" if ledger["pending"] else "")
    )

    lines.append("")
    for transform in PIPELINE:
        if transform not in _PROPOSAL_FILES:
            lines.append(f"{transform:<12} (no proposal file — decisions live "
                         "in its run log)")
            continue
        summary = data["proposals"][transform]
        if summary is None:
            lines.append(f"{transform:<12} not mined")
            continue
        counts = ", ".join(
            f"{n} {status}" for status, n in sorted(summary["by_status"].items())
        ) or "0 proposals"
        orphaned = (f" · {summary['orphaned']} ORPHANED (review them)"
                    if summary["orphaned"] else "")
        lines.append(f"{transform:<12} {counts}{orphaned}")

    recorded = {e["transform"] for e in data["gates"]["entries"]}
    unrecorded = [t for t in PIPELINE
                  if data["artifacts"].get(t) and t not in recorded]
    if unrecorded:
        lines.append("")
        lines.append(
            f"note: run artifacts exist for {', '.join(unrecorded)} with no "
            "gate-ledger entry — a dry run, or an apply that predates the "
            "ledger. The ledger vouches only for what it saw; the journal "
            "holds the history."
        )

    return "\n".join(lines)
