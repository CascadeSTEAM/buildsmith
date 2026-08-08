"""Collapse — remove provably-inert wrappers from the div/span soup.

Runs BEFORE componentize (ADR-009): normalizing near-identical subtrees is
what makes repeated-structure detection work, and this way the collapse
never has to reason about component boundaries — it refuses them outright.

v1 is deliberately conservative. A block is removed only when the structural
argument holds:

  - tag div or span, with exactly one child block OF THE SAME TAG (promoting
    a different tag into the slot would shift :nth-of-type/:first-child
    matches among siblings);
  - no styles in any style dict (base/mobile/tablet/raw);
  - no innerHTML, no attributes or customAttributes that survive rendering
    (a minted class alone is allowed — but only when no client script
    depends on it, per the baseline's scripts-scan);
  - not a component reference, extension, or repeater, and not the root.

That argument is a FILTER, not a proof. Positional CSS in client scripts
(`.card > div`, `:nth-of-type`, descendant depth) references structure by
shape, not by name, and no name-token scan can see it. The rendering oracle
is therefore the actual equivalence proof (ADR-009), and the CLI runs it as
part of --apply — a collapse whose oracle has not passed has not passed.

The style-merging variant (folding a styled wrapper into its child) is
deferred on purpose: CSS composition is not associative — padding vs margin,
flex context, margin collapse — and "probably equivalent" is exactly what
this phase must never ship.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from buildsmith.workflows.optimize.tokenize import (
    STYLE_KEYS,
    _refuse_stale_checkpoint,
    _select,
    load_state,
    walk,
)

ROOT = Path(__file__).resolve().parents[3]

_MINTED = re.compile(r"^(?:fb|bldr)-[0-9a-f]{4,}$")

#: block keys that make a wrapper load-bearing regardless of anything else
_STRUCTURAL_KEYS = ("extendedFromComponent", "isChildOfComponent",
                    "isRepeaterBlock", "dataKey", "visibilityCondition")


def _protected_names(scan: dict) -> set[str]:
    """Every class/id/selector token a client script is known to touch."""
    protected: set[str] = set()
    for script in scan.get("scripts", []):
        touches = script.get("touches", {})
        for key in ("ids", "classes", "class_ops", "minted_classes"):
            protected.update(touches.get(key, []))
        for selector in touches.get("selectors", []):
            protected.update(re.findall(r"[.#]([\w-]+)", selector))
    return protected


def removable(block: dict, *, protected: set[str]) -> str:
    """Why this wrapper is provably inert, or '' if it is not."""
    element = (block.get("element") or "").lower()
    if element not in ("div", "span"):
        return ""
    children = block.get("children") or []
    if len(children) != 1:
        return ""
    if (children[0].get("element") or "").lower() != element:
        # promoting a different tag shifts type-positional CSS
        # (:nth-of-type, :first-child) among this block's siblings
        return ""
    if block.get("innerHTML"):
        return ""
    for key in _STRUCTURAL_KEYS:
        if block.get(key):
            return ""
    for key in STYLE_KEYS:
        if block.get(key):
            return ""
    attributes = dict(block.get("attributes") or {})
    custom = block.get("customAttributes") or {}
    if custom:
        return ""
    classes = [c for c in (block.get("classes") or []) if c]
    named = [c for c in classes if not _MINTED.match(c)]
    if named:
        return ""
    block_id = str(block.get("blockId") or "")
    touched = [c for c in classes if c in protected]
    if touched or (attributes.get("id") and attributes["id"] in protected) \
            or block_id in protected:
        return ""
    leftover = {k: v for k, v in attributes.items() if k != "class"}
    if leftover:
        return ""
    return ("bare single-child wrapper: no styles, no content, no "
            "surviving attributes, no script dependencies")


def collapse_tree(roots: list[dict], *, protected: set[str],
                  max_passes: int = 10) -> list[dict]:
    """Remove inert wrappers in place; return the merge log.

    The single child takes its parent's slot. Roots are never removed —
    Builder treats the root specially and a page needs one.
    """
    log: list[dict] = []

    def safe_walk(block):
        """walk(), but never descend below a component reference: the blocks
        under one are override shells mirrored by blockId (TRAP-001), and
        a 'bare' shell is load-bearing even though it looks inert."""
        yield block
        if block.get("extendedFromComponent"):
            return
        for child in block.get("children") or []:
            yield from safe_walk(child)

    def one_pass() -> int:
        removed = 0
        for root in roots:
            for block in safe_walk(root):
                if block.get("extendedFromComponent"):
                    continue
                children = block.get("children") or []
                replaced = []
                for child in children:
                    reason = removable(child, protected=protected)
                    if reason:
                        grandchild = child["children"][0]
                        log.append({
                            "removed": child.get("blockId"),
                            "kept": grandchild.get("blockId"),
                            "under": block.get("blockId"),
                            "proof": reason,
                        })
                        replaced.append(grandchild)
                        removed += 1
                    else:
                        replaced.append(child)
                if children:
                    block["children"] = replaced
        return removed

    for _ in range(max_passes):
        if not one_pass():
            break
    return log


def run(site: str, *, target: str = "sandbox.localhost",
        routes: list[str] | None = None, apply: bool = False,
        runner=None) -> dict:
    """Collapse the selected trees; report always, write back only on apply."""
    from buildsmith.tools.sandbox import run_bench

    scan_path = ROOT / "sites" / site / "opt" / "baseline" / \
        "scripts-scan.json"
    if not scan_path.exists():
        raise SystemExit(f"no scripts scan at {scan_path} — run "
                         "`buildsmith optimize baseline` first; collapsing "
                         "without it could break a client script silently")
    protected = _protected_names(json.loads(scan_path.read_text()))

    warnings: list[str] = []
    if apply:
        _refuse_stale_checkpoint(site, target=target)
    else:
        # a dry run against a stale checkpoint reports wrappers that may no
        # longer exist — warn rather than refuse, so it still works offline
        try:
            _refuse_stale_checkpoint(site, target=target)
        except SystemExit as exc:
            warnings.append(f"checkpoint freshness not verified: {exc}")
    pages, components = load_state(site)
    selected = _select(site, pages, components, routes)
    if not selected:
        raise SystemExit(f"REFUSED: route filter {routes!r} selected no pages")

    out_dir = ROOT / "sites" / site / "opt" / "transforms" / "collapse"
    if out_dir.exists():
        shutil.rmtree(out_dir)   # reports describe THIS run, never a prior one
    out_dir.mkdir(parents=True)
    updates: dict[str, dict] = {}
    logs: dict[str, list] = {}
    components_skipped: list[str] = []
    for label, roots in selected.items():
        if label.startswith("component:"):
            # never collapse inside a component tree here: pages mirror
            # component blockIds in their override shells (TRAP-001), and
            # this transform does not manage that mirror. Recorded, not
            # silent — the soup inside a component stays until a transform
            # that manages the mirror exists.
            components_skipped.append(label.partition(":")[2])
            continue
        before = sum(1 for r in roots for _ in walk(r))
        log = collapse_tree(roots, protected=protected)
        if log:
            kind, _, name = label.partition(":")
            after = sum(1 for r in roots for _ in walk(r))
            updates[label] = {"kind": kind, "name": name, "blocks": roots}
            logs[label] = log
            (out_dir / f"{kind}-{name}.json").write_text(json.dumps(
                {"before": before, "after": after, "log": log,
                 "blocks": roots}, indent=2) + "\n")

    total = sum(len(v) for v in logs.values())
    if apply and updates:
        # Ledger before mutation — library-level so no caller skips it.
        from buildsmith.workflows.optimize import gates

        gates.record_apply(site, "collapse", target=target)
        run_fn = runner or run_bench
        script = f"""
import frappe, json
frappe.init({json.dumps(target)}); frappe.connect()
frappe.flags.in_import = True
for item in json.loads({json.dumps(json.dumps(updates))}).values():
    doc = frappe.get_doc('Builder Page', item['name'])
    doc.blocks = json.dumps(item['blocks'])
    doc.save()
frappe.db.commit()
from frappe.website.utils import clear_website_cache
clear_website_cache()
print('applied', len(json.loads({json.dumps(json.dumps(updates))})))
"""
        run_fn(script)

    return {"removed": total, "targets": sorted(updates),
            "log_dir": str(out_dir),
            "components_skipped": sorted(components_skipped),
            "warnings": warnings,
            "apply_requested": bool(apply),
            "applied": bool(apply and updates)}
