#!/usr/bin/env python3
"""Dry-run a component payload against a state export. TRAP-001.

The most expensive near-miss on record: replacing an in-use component's `block`
with a freshly-composed tree would have wiped the header and footer across all
13 pages of a live site. It was caught only because someone simulated first.
This makes that structural.

**What it reproduces.** Pages do not embed components. Each carries a mirror of
empty override shells, and `extend_block()` rebuilds the visible tree at render
time by iterating the *page's* shells and matching each against the component's
children on `blockId` ∈ (shell.blockId, shell.referenceBlockId). A shell that
matches nothing is emitted exactly as it is — `element=None`, no content. That
loop is reproduced here line for line from the pinned Builder.

**Why it compares rather than counts.** A page can already contain shells that
match nothing, from some earlier change. Reporting those as failures would blame
this payload for damage it did not cause, and — worse — train everyone to ignore
the output. So it simulates the *current* component and the *proposed* one, and
reports only what changes:

    collapse    matched before, matches nothing after   <- caused by this payload
    pre-existing  matched nothing either way            <- already broken, reported quietly
    unrenderable  a component child no shell references <- will not appear on this page

**Pinned pages are excluded.** A page block carrying `componentVersion` resolves
against a Builder Snapshot, not the live component, so a change to the live
record cannot affect it. Counting those would be a false positive.

Usage:
    buildsmith simulate --state state-export.json --payload component.json
    buildsmith simulate --state state-export.json --payload one.json --payload two.json

Exit status is 1 if any page would collapse, 0 otherwise. Nothing here connects
to a site; the state export is a file, read back beforehand.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

__all__ = [
    "Finding",
    "Report",
    "load_state",
    "pages_using",
    "simulate",
]


@dataclass(frozen=True)
class Finding:
    """One node that would render differently, on one page."""

    page: str
    route: str
    path: str
    block_id: str
    kind: str  # collapse | pre-existing | unrenderable
    detail: str = ""
    descendants: int = 0

    def __str__(self) -> str:
        extra = f" (+{self.descendants} descendant(s))" if self.descendants else ""
        return f"{self.route or self.page}  {self.path}  [{self.block_id}]{extra}  {self.detail}"


@dataclass
class Report:
    collapses: list[Finding] = field(default_factory=list)
    pre_existing: list[Finding] = field(default_factory=list)
    unrenderable: list[Finding] = field(default_factory=list)
    pinned_pages: list[str] = field(default_factory=list)
    pages_checked: int = 0
    components_checked: list[str] = field(default_factory=list)
    #: Set when the export contained no pages at all. The run then proves
    #: nothing, and a pass that proves nothing must not read like a pass.
    vacuous: bool = False

    @property
    def ok(self) -> bool:
        """No collapses — and a run that checked nothing does not count as clean."""
        return not self.collapses and not self.vacuous

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "vacuous": self.vacuous,
            "pages_checked": self.pages_checked,
            "components_checked": self.components_checked,
            "pinned_pages_skipped": self.pinned_pages,
            "collapses": [f.__dict__ for f in self.collapses],
            "pre_existing": [f.__dict__ for f in self.pre_existing],
            "unrenderable": [f.__dict__ for f in self.unrenderable],
        }

    def summary(self) -> str:
        lines = [
            f"simulate: {len(self.components_checked)} component(s) against "
            f"{self.pages_checked} consuming page(s)"
        ]
        if self.pinned_pages:
            lines.append(
                f"  {len(self.pinned_pages)} page(s) pinned to a component version, "
                f"unaffected: {', '.join(sorted(set(self.pinned_pages)))}"
            )
        if self.collapses:
            lines.append(f"  COLLAPSE — {len(self.collapses)} node(s) this payload would break:")
            lines += [f"    {f}" for f in self.collapses]
        if self.unrenderable:
            lines.append(
                f"  {len(self.unrenderable)} component node(s) no page shell references — "
                "they will not render until a ComponentSyncer pass runs:"
            )
            lines += [f"    {f}" for f in self.unrenderable]
        if self.pre_existing:
            lines.append(
                f"  {len(self.pre_existing)} node(s) already matched nothing before this "
                "payload — pre-existing, not caused by it:"
            )
            lines += [f"    {f}" for f in self.pre_existing]
        if self.vacuous:
            lines.append(
                "  NOTHING WAS CHECKED — the state export contains no pages, so this run "
                "proves nothing. Read the pages back from the site."
            )
        elif self.ok and not self.unrenderable:
            lines.append("  clean — every existing shell still matches")
        return "\n".join(lines)


def _as_list(blocks: Any) -> list[dict]:
    if blocks is None:
        return []
    if isinstance(blocks, str):
        blocks = json.loads(blocks)
    if isinstance(blocks, dict):
        return [blocks]
    return [b for b in blocks if b]


def _match(component_children: list[dict], shell: dict) -> dict | None:
    """`extend_block`'s matching rule, reproduced exactly."""
    wanted = (shell.get("blockId"), shell.get("referenceBlockId"))
    return next((c for c in component_children if c.get("blockId") in wanted), None)


def _count_nodes(block: dict) -> int:
    return 1 + sum(_count_nodes(c) for c in (block.get("children") or []) if c)


def _compare_children(
    current: dict,
    proposed: dict,
    shell: dict,
    *,
    path: str,
    on_finding,
) -> None:
    """Walk a page's shells against both component trees, reporting differences."""
    current_children = current.get("children") or []
    proposed_children = proposed.get("children") or []
    shell_children = shell.get("children") or []

    for index, child_shell in enumerate(shell_children):
        if not child_shell:
            continue
        block_id = child_shell.get("referenceBlockId") or child_shell.get("blockId") or "?"
        child_path = f"{path}/children[{index}]"

        matched_before = _match(current_children, child_shell)
        matched_after = _match(proposed_children, child_shell)

        if matched_after is not None:
            # Still matches: recurse, exactly as extend_block does.
            _compare_children(
                matched_before or matched_after,
                matched_after,
                child_shell,
                path=child_path,
                on_finding=on_finding,
            )
        elif matched_before is not None:
            on_finding(
                "collapse",
                child_path,
                block_id,
                "matched a component child before, matches nothing after — renders as "
                "element=None",
                _count_nodes(child_shell) - 1,
            )
        else:
            on_finding(
                "pre-existing",
                child_path,
                block_id,
                "matched nothing before this payload either",
                0,
            )

    # Component children no shell points at never render on this page.
    shell_keys = {
        key
        for child in shell_children
        if child
        for key in (child.get("blockId"), child.get("referenceBlockId"))
        if key
    }
    for index, proposed_child in enumerate(proposed_children):
        if proposed_child.get("blockId") not in shell_keys:
            on_finding(
                "unrenderable",
                f"{path}/children[{index}]",
                proposed_child.get("blockId") or "?",
                f"<{proposed_child.get('element', '?')}> is in the component but no shell "
                "on this page references it",
                _count_nodes(proposed_child) - 1,
            )


_BUCKETS = {
    "collapse": "collapses",
    "pre-existing": "pre_existing",
    "unrenderable": "unrenderable",
}


def _recorder(report: Report, page: str, route: str):
    """Bind a report bucket to one page, so the walker stays page-agnostic."""

    def record(kind: str, path: str, block_id: str, detail: str, descendants: int) -> None:
        getattr(report, _BUCKETS[kind]).append(
            Finding(
                page=page,
                route=route,
                path=path,
                block_id=block_id,
                kind=kind,
                detail=detail,
                descendants=descendants,
            )
        )

    return record


def pages_using(pages: list[dict], component_id: str) -> list[dict]:
    """Pages whose blocks reference the component, at any depth.

    Reproduces `builder.utils.is_component_used`, including that it checks
    `draft_blocks` as well — an unpublished draft is still damage waiting to be
    published.
    """

    def used(blocks: Any) -> bool:
        for block in _as_list(blocks):
            if block.get("extendedFromComponent") == component_id:
                return True
            if block.get("children") and used(block["children"]):
                return True
        return False

    return [
        page
        for page in pages
        if used(page.get("blocks")) or used(page.get("draft_blocks"))
    ]


def _find_component_roots(blocks: Any, component_id: str, path: str = "blocks"):
    """Yield (path, shell) for every node extending the component."""
    for index, block in enumerate(_as_list(blocks)):
        here = f"{path}[{index}]"
        if block.get("extendedFromComponent") == component_id:
            yield here, block
        if block.get("children"):
            yield from _find_component_roots(block["children"], component_id, f"{here}/children")


def simulate(state: dict, payloads: list[dict]) -> Report:
    """Simulate proposed component payloads against a state export."""
    report = Report()
    components = state.get("components") or {}
    pages = state.get("pages") or []
    report.vacuous = not pages

    for payload in payloads:
        component_id = payload.get("component_id") or payload.get("name")
        if not component_id:
            raise ValueError("a payload needs a component_id")
        report.components_checked.append(component_id)

        current = components.get(component_id)
        if current is None:
            # A component absent from the export is either brand new — nothing
            # live to break — or the export is incomplete. Those look identical
            # here and only one of them is safe, so check before assuming.
            orphaned = pages_using(pages, component_id)
            if orphaned:
                raise ValueError(
                    f"'{component_id}' is not in the state export's components, but "
                    f"{len(orphaned)} page(s) already use it "
                    f"({', '.join(p.get('name', '?') for p in orphaned[:3])}). The export is "
                    "incomplete, so simulating it would compare against nothing and pass "
                    "vacuously — which is indistinguishable from a clean run. Re-read the "
                    "component back from the site."
                )
            continue
        current_block = current.get("block") if isinstance(current, dict) else current
        current_block = (json.loads(current_block)
                         if isinstance(current_block, str) else current_block)
        proposed_block = payload.get("block") or {}
        proposed_block = (
            json.loads(proposed_block) if isinstance(proposed_block, str) else proposed_block
        )

        consuming = pages_using(pages, component_id)
        report.pages_checked += len(consuming)

        for page in consuming:
            page_name = page.get("name", "?")
            route = page.get("route", "")

            for source in ("blocks", "draft_blocks"):
                for path, shell in _find_component_roots(page.get(source), component_id, source):
                    # A shell pinned to a version resolves against a snapshot, so
                    # the live component changing cannot affect it.
                    if shell.get("componentVersion"):
                        report.pinned_pages.append(page_name)
                        continue

                    _compare_children(
                        current_block,
                        proposed_block,
                        shell,
                        path=path,
                        on_finding=_recorder(report, page_name, route),
                    )

    return report


def load_state(path: str | Path) -> dict:
    """Load a state export.

    Expected shape — everything read back from the site beforehand:

        {"components": {"<component_id>": {"block": {...}}},
         "pages": [{"name": ..., "route": ..., "blocks": [...], "draft_blocks": [...]}]}
    """
    state = json.loads(Path(path).read_text())
    if "pages" not in state:
        raise ValueError(
            f"{path}: a state export needs a 'pages' list. Without the pages there is "
            "nothing to simulate against, and an empty simulation passes vacuously."
        )
    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--state", required=True, help="state export read back from the site")
    parser.add_argument(
        "--payload", required=True, action="append", help="component payload (repeatable)"
    )
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)

    state = load_state(args.state)
    payloads = [json.loads(Path(p).read_text()) for p in args.payload]

    report = simulate(state, payloads)
    print(json.dumps(report.to_dict(), indent=2) if args.json else report.summary())

    if report.vacuous:
        # Nothing was simulated, so nothing was proved — and saying "refusing:
        # collapse" here would be a lie in the other direction. Exit 2, the
        # code for "could not check", not 1.
        print(
            "\nNOTHING CHECKED — the export contained no pages, so no consumer "
            "was simulated. Not a pass; re-read the export from the site.",
            file=sys.stderr,
        )
        return 2
    if not report.ok:
        print(
            "\nRefusing: this payload would collapse nodes on pages already using the "
            "component. Preserve the blockIds and restyle in place, or arrange a "
            "ComponentSyncer pass across every consuming page in the same operation "
            "(TRAP-001).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
