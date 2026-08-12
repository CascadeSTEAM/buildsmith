"""Componentize, half one: FIND the repeated structures and propose them.

Detection is shape-hashing: a block's shape is its element, its sorted style
property names (values excluded — two menu cards differ in colour value but
share a shape), its class-shape, and its children's shapes, recursively.
blockIds, text content and attribute values are excluded on purpose: those
are exactly what varies between instances of the same structure.

The output is a proposal file (`opt/proposals/components.json`) — the
persisted decision artifact (ADR-009). Each candidate carries the shape's
occurrence count, where the instances live, its size in blocks, and a
`status` a human flips to "accepted".

Half two, `apply()`, turns an accepted proposal into a Builder Component and
rewrites every instance into an override shell — TRAP-001 territory, so it
goes through `primitives/components.py`'s `compose()`/`override_shells()`
and ends in the rendering oracle, same as every other Phase A transform.
It is scoped to the case an accepted shape's instances are already
content-identical: nothing distinguishes them, so the shell each becomes is
legitimately empty. A shape with genuinely divergent per-instance content
(different text, links, or styles between occurrences) is reported and left
`accepted`, not applied and not guessed at — extracting a shape like that
needs to construct real per-instance overrides, which is separate, harder
work (issue #19's proposal comment).

Candidates are reported largest-first: extracting the 12-block card repeated
40 times is the win; the 2-block pair repeated twice is noise. Nested
candidates are pruned — a shape wholly contained in a bigger reported shape
is not separately proposed.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from buildsmith.errors import CouldNotCheck
from buildsmith.primitives.blocks import walk
from buildsmith.workflows.optimize.tokenize import (
    _refuse_stale_checkpoint,
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

    An `extendedFromComponent` block is treated as childless here, mirroring
    collapse's `safe_walk`: its children are override shells belonging to
    whatever component it already extends (TRAP-001), not free structure of
    this page. Descending into them would let this shape-hash function
    single out a shell subtree — often several near-empty, coincidentally
    identical placeholder nodes — and propose it for extraction, which is
    exactly the corruption TRAP-001 warns about, compounded: a fresh
    component minted over another component's mirror.
    """
    shapes: dict[int, tuple[str, int]] = {}
    stack: list[tuple[dict, bool]] = [(root, False)]
    while stack:
        block, ready = stack.pop()
        children = ([] if block.get("extendedFromComponent")
                    else block.get("children") or [])
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
                # matches annotate()'s boundary: an extended block's shells
                # were never hashed, so their ids are absent from `shapes`
                # and pushing them here would KeyError — and they must not
                # be walked into regardless (TRAP-001).
                if block.get("extendedFromComponent"):
                    continue
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


def _content_signature(block: dict) -> tuple:
    """A hashable fingerprint of everything an empty shell cannot preserve.

    Broader than `components.assert_content_preserved`'s innerHTML/href/src:
    that guard exists to catch content a *revision* dropped, not to prove two
    *instances* are safe to collapse into one bare shell — a style difference
    between two instances (say, one card's accent colour) is just as much a
    thing an empty shell would silently erase as a text difference is.
    blockId is excluded on purpose: it is identity, expected to differ, and
    is exactly what `override_shells()` preserves per instance regardless.
    """
    return tuple(
        (
            node.get("element"),
            node.get("innerHTML"),
            json.dumps(node.get("attributes") or {}, sort_keys=True),
            json.dumps(node.get("customAttributes") or {}, sort_keys=True),
            json.dumps(node.get("baseStyles") or {}, sort_keys=True),
            json.dumps(node.get("mobileStyles") or {}, sort_keys=True),
            json.dumps(node.get("tabletStyles") or {}, sort_keys=True),
            json.dumps(node.get("rawStyles") or {}, sort_keys=True),
            tuple(sorted(node.get("classes") or [])),
            json.dumps(node.get("dynamicValues") or [], sort_keys=True),
            json.dumps(node.get("visibilityCondition"), sort_keys=True),
            bool(node.get("isRepeaterBlock")),
        )
        for node in walk(block)
    )


def _find_by_block_id(roots: list[dict], block_id: str) -> dict | None:
    """The node with this blockId under one of `roots`'s trees, or None."""
    for root in roots:
        for node in walk(root):
            if node.get("blockId") == block_id:
                return node
    return None


def _replace_by_block_id(roots: list[dict], block_id: str,
                         replacement: dict) -> bool:
    """Swap the node with this blockId for `replacement`, in place."""
    for index, root in enumerate(roots):
        if root.get("blockId") == block_id:
            roots[index] = replacement
            return True
        if _replace_in_children(root, block_id, replacement):
            return True
    return False


def _replace_in_children(node: dict, block_id: str, replacement: dict) -> bool:
    children = node.get("children")
    if not children:
        return False
    for index, child in enumerate(children):
        if child.get("blockId") == block_id:
            children[index] = replacement
            return True
        if _replace_in_children(child, block_id, replacement):
            return True
    return False


def accepted_proposals(site: str) -> list[dict]:
    """Accepted, named proposals from the decision record. Refuses ambiguity,
    mirroring `tokenize.accepted_mapping`."""
    path = proposal_path(site)
    if not path.exists():
        raise CouldNotCheck(f"no proposal file at {path} — mine first")
    data = json.loads(path.read_text())
    accepted = [p for p in data["proposals"] if p["status"] == "accepted"]
    unnamed = [p["shape"] for p in accepted if not p["name"]]
    if unnamed:
        raise SystemExit(
            f"accepted but unnamed: {unnamed} — a component needs a "
            "human-given name (a lower-case hyphenated slug, e.g. "
            "'site-header') before it becomes a record")
    names = [p["name"] for p in accepted]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise SystemExit(f"duplicate component names: {sorted(dupes)}")
    return accepted


def apply(site: str, *, clone_url: str = "http://127.0.0.1:8000",
          target: str = "sandbox.localhost", runner=None) -> dict:
    """Apply accepted proposals whose every instance is content-identical to
    its exemplar: mint a `Builder Component`, replace each instance with an
    override shell, write back, and let the oracle prove nothing rendered
    differently.

    A proposal whose instances disagree on content or style is reported in
    `skipped` and left untouched (`status` stays "accepted") — see the
    module docstring for why this is a deliberate scope cut, not a bug.
    """
    from buildsmith.primitives.components import (
        ComponentError,
        compose,
        override_shells,
        slug_to_component_id,
    )
    from buildsmith.tools.sandbox import run_bench

    accepted = accepted_proposals(site)
    if not accepted:
        raise CouldNotCheck("no accepted proposals — nothing to apply")

    for proposal in accepted:
        try:
            slug_to_component_id(proposal["name"])
        except ComponentError as exc:
            raise SystemExit(f"{proposal['shape']}: {exc}") from exc

    # staleness guard: extraction sources block trees from the baseline
    # checkpoint, same reason tokenize refuses against a stale one.
    _refuse_stale_checkpoint(site, target=target)

    pages, _components = load_state(site)

    ready: list[dict] = []
    skipped: list[dict] = []
    for proposal in accepted:
        instances = proposal["instances"]
        located: list[tuple[str, str, dict]] = []
        missing = []
        for instance in instances:
            label = instance["label"]
            page_name = label.partition(":")[2]
            roots = pages.get(page_name)
            node = _find_by_block_id(roots, instance["blockId"]) if roots else None
            if node is None:
                missing.append(f"{label}#{instance['blockId']}")
                continue
            located.append((page_name, instance["blockId"], node))
        if missing:
            skipped.append({
                "shape": proposal["shape"], "name": proposal["name"],
                "reason": f"{len(missing)} instance(s) not found in the "
                         f"current checkpoint (stale proposal?): {missing[:3]}",
            })
            continue

        exemplar = located[0][2]
        signature = _content_signature(exemplar)
        diverged = [f"{page}#{block_id}" for page, block_id, node in located[1:]
                   if _content_signature(node) != signature]
        if diverged:
            skipped.append({
                "shape": proposal["shape"], "name": proposal["name"],
                "reason": "instance(s) carry different content or styles "
                         f"than the exemplar — not applied: {diverged[:3]}"
                         f"{'...' if len(diverged) > 3 else ''}. Extracting "
                         "this shape needs per-instance override "
                         "construction, which this apply does not do yet.",
            })
            continue

        ready.append({"proposal": proposal, "exemplar": exemplar,
                      "located": located})

    out_dir = ROOT / "sites" / site / "opt" / "transforms" / "componentize"
    out_dir.mkdir(parents=True, exist_ok=True)

    components_payload: dict[str, dict] = {}
    touched_pages: set[str] = set()
    applied_shapes: list[str] = []
    for item in ready:
        proposal, exemplar = item["proposal"], item["exemplar"]
        component_id = proposal["name"]
        component_name = component_id.replace("-", " ").title()

        composed = compose(
            component_id=component_id, component_name=component_name,
            root=exemplar,
        )
        for page_name, block_id, node in item["located"]:
            shell = override_shells(composed.block, node,
                                    component_id=component_id)
            if not _replace_by_block_id(pages[page_name], block_id, shell):
                raise SystemExit(
                    f"REFUSED: {page_name}#{block_id} vanished between "
                    "locating it and writing it back — this is a bug")
            touched_pages.add(page_name)

        components_payload[component_id] = composed.record()
        applied_shapes.append(proposal["shape"])
        (out_dir / f"component-{component_id}.json").write_text(
            json.dumps(composed.record(), indent=2) + "\n")

    pages_payload = {name: pages[name] for name in sorted(touched_pages)}
    for name, roots in pages_payload.items():
        (out_dir / f"page-{name}.json").write_text(
            json.dumps(roots, indent=2) + "\n")

    if components_payload:
        from buildsmith.workflows.optimize import gates

        # Ledger before mutation — library-level so no caller can mutate the
        # sandbox without a pending entry, same as every other transform.
        gates.record_apply(site, "componentize", target=target)

        run_fn = runner or run_bench
        script = f"""
import frappe, json
frappe.init({json.dumps(target)}); frappe.connect()
frappe.flags.in_import = True

components = json.loads({json.dumps(json.dumps(components_payload))})
collisions = [cid for cid in components
              if frappe.db.exists('Builder Component', {{'component_id': cid}})]
if collisions:
    print(json.dumps({{'created': [], 'collisions': collisions}}))
else:
    for cid, payload in components.items():
        payload['block'] = json.dumps(payload['block'])
        frappe.get_doc(payload).insert()
    pages = json.loads({json.dumps(json.dumps(pages_payload))})
    for name, roots in pages.items():
        doc = frappe.get_doc('Builder Page', name)
        doc.blocks = json.dumps(roots)
        doc.save()
    frappe.db.commit()
    from frappe.website.utils import clear_website_cache
    clear_website_cache()
    print(json.dumps({{'created': sorted(components), 'collisions': []}}))
"""
        result = json.loads(run_fn(script).strip().splitlines()[-1])
        if result["collisions"]:
            raise SystemExit(
                "REFUSED: component_id already exists on the site, not "
                f"minted by this apply: {sorted(result['collisions'])}. "
                "Nothing was written — rename the proposal or investigate "
                "the existing record before retrying.")

    if applied_shapes:
        path = proposal_path(site)
        data = json.loads(path.read_text())
        for p in data["proposals"]:
            if p["shape"] in applied_shapes:
                p["status"] = "applied"
        path.write_text(json.dumps(data, indent=2) + "\n")

    return {
        "applied": sorted(applied_shapes),
        "targets": sorted(touched_pages),
        "skipped": skipped,
    }
