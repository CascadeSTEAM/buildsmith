"""Componentize, half one: FIND the repeated structures and propose them.

Detection is shape-hashing: a block's shape is its element, its sorted style
property names (values excluded — two menu cards differ in colour value but
share a shape), its class-shape, and its children's shapes, recursively.
blockIds, text content and attribute values are excluded on purpose: those
are exactly what varies between instances of the same structure.

The output is a proposal file (`opt/proposals/components.json`) — the
persisted decision artifact (ADR-009). Each candidate carries the shape's
occurrence count, where the instances live, its size in blocks, and a
`status` a human flips to "accepted". Nothing here writes to the sandbox:
the apply half is a separate step, because turning an accepted proposal into
a Builder Component and rewriting pages to extend it is TRAP-001 territory
(override shells mirrored by blockId) and goes through
`primitives/components.py`, `simulate`, and the oracle.

Candidates are reported largest-first: extracting the 12-block card repeated
40 times is the win; the 2-block pair repeated twice is noise. Nested
candidates are pruned — a shape wholly contained in a bigger reported shape
is not separately proposed.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from buildsmith.workflows.optimize.tokenize import (
    _select,
    load_state,
)

ROOT = Path(__file__).resolve().parents[3]

#: a candidate must repeat at least this often…
MIN_OCCURRENCES = 3
#: …and each instance must be at least this many blocks to matter
MIN_BLOCKS = 4


def annotate(root: dict) -> dict[int, tuple[str, int]]:
    """{id(block): (shape hash, block count)} for every block, iteratively.

    Post-order with an explicit stack: a scraped page can be a single-child
    chain hundreds deep, and recursion + hashing-from-scratch at every node
    is both fragile and O(n^2). The payload is JSON, not delimiter-joined —
    a class name containing '.' or '|' must not collide two different
    structures into one shape.
    """
    shapes: dict[int, tuple[str, int]] = {}
    stack: list[tuple[dict, bool]] = [(root, False)]
    while stack:
        block, ready = stack.pop()
        children = block.get("children") or []
        if not ready:
            stack.append((block, True))
            for child in children:
                stack.append((child, False))
            continue
        count = 1
        child_shapes = []
        for child in children:
            child_hash, child_count = shapes[id(child)]
            child_shapes.append(child_hash)
            count += child_count
        style_shape = [sorted((block.get(key) or {}).keys())
                       for key in ("baseStyles", "mobileStyles",
                                   "tabletStyles", "rawStyles")]
        named_classes = sorted(
            c for c in (block.get("classes") or [])
            if c and not c.startswith(("fb-", "bldr-")))
        payload = json.dumps([
            (block.get("element") or "?").lower(),
            style_shape,
            named_classes,
            bool(block.get("extendedFromComponent")),
            bool(block.get("isRepeaterBlock")),
            child_shapes,
        ], separators=(",", ":"))
        digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
        shapes[id(block)] = (digest, count)
    return shapes


def shape_of(block: dict) -> tuple[str, int]:
    """(shape hash, block count) for the subtree rooted here."""
    return annotate(block)[id(block)]


def find_candidates(trees: dict[str, list[dict]]) -> list[dict]:
    """Repeated shapes worth extracting, largest first.

    Pruning is per-INSTANCE, not per-shape: an instance nested inside a
    kept, bigger candidate is dropped (its blocks are already spoken for),
    but free-standing instances of the same shape still count — and only
    shapes with enough FREE instances are reported, so totals never
    double-count a block.
    """
    instances: dict[str, list[dict]] = {}
    sizes: dict[str, int] = {}

    for label, roots in trees.items():
        for root in roots:
            shapes = annotate(root)
            stack: list[tuple[dict, list[str]]] = [(root, [])]
            while stack:
                block, ancestors = stack.pop()
                digest, count = shapes[id(block)]
                sizes[digest] = count
                instances.setdefault(digest, []).append(
                    {"label": label, "blockId": block.get("blockId"),
                     "element": block.get("element"),
                     "ancestors": list(ancestors)})
                for child in block.get("children") or []:
                    stack.append((child, ancestors + [digest]))

    eligible = {
        digest: occ for digest, occ in instances.items()
        if len(occ) >= MIN_OCCURRENCES and sizes[digest] >= MIN_BLOCKS
    }
    kept: list[str] = []
    result = []
    for digest in sorted(eligible, key=lambda d: -sizes[d]):
        occ = eligible[digest]
        free = [inst for inst in occ
                if not any(a in kept for a in inst["ancestors"])]
        if len(free) < MIN_OCCURRENCES:
            continue
        kept.append(digest)
        result.append({
            "shape": digest,
            "blocks_per_instance": sizes[digest],
            "occurrences": len(free),
            "nested_pruned": len(occ) - len(free),
            "total_blocks": sizes[digest] * len(free),
            "element": free[0]["element"],
            "where": sorted({i["label"] for i in free}),
            "instances": [{"label": i["label"], "blockId": i["blockId"]}
                          for i in free],
            "instance_block_ids": [i["blockId"] for i in free],
        })
    result.sort(key=lambda c: -c["total_blocks"])
    return result


def proposal_path(site: str) -> Path:
    return ROOT / "sites" / site / "opt" / "proposals" / "components.json"


def mine(site: str, *, routes: list[str] | None = None) -> dict:
    """(Re)write the component proposal file, preserving human decisions.

    A prior decision (anything named or moved past "proposed") whose shape
    no longer appears is NOT dropped: it moves to `orphaned` in the file,
    because a decision that silently vanishes was never a record. Shape
    hashes change when the underlying structure changes (a collapse, an
    editor edit) — orphaning is expected life-cycle, losing it is not.
    """
    pages, components = load_state(site)
    selected = _select(site, pages, components, routes)
    # detection runs over pages only: what is already a component does not
    # need proposing, and shells under a component ref belong to it
    page_trees = {k: v for k, v in selected.items() if k.startswith("page:")}
    if routes is not None and not page_trees:
        raise SystemExit(
            f"REFUSED: route filter {routes!r} selected no pages — a filter "
            "matching nothing must not read as 'no repeats found'")
    candidates = find_candidates(page_trees)

    path = proposal_path(site)
    prior: dict[str, dict] = {}
    prior_orphans: list[dict] = []
    if path.exists():
        old = json.loads(path.read_text())
        prior = {p["shape"]: p for p in old.get("proposals", [])}
        prior_orphans = old.get("orphaned", [])
    proposals = []
    for candidate in candidates:
        kept = prior.pop(candidate["shape"], {})
        proposals.append({
            **candidate,
            "name": kept.get("name", ""),
            "status": kept.get("status", "proposed"),
        })
    decided = [p for p in prior.values()
               if p.get("name") or p.get("status") not in (None, "proposed")]
    orphaned = prior_orphans + decided
    data = {
        "_meta": {
            "site": site,
            "note": "Name a candidate and flip status to \"accepted\" to "
                    "queue it for extraction. The apply half is a separate "
                    "step (TRAP-001 machinery); this file is the decision "
                    "record either way. `orphaned` holds past decisions "
                    "whose shape no longer exists — review, then delete "
                    "them by hand.",
        },
        "proposals": proposals,
        "orphaned": orphaned,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return data
