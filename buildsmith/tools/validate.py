#!/usr/bin/env python3
"""Validate emitted payloads before anyone applies them.

The last gate on the artifacts side. Everything here is also enforced at
construction time by `primitives/`, so a payload built through this project
should always pass — which is the point. This catches the payload that did
*not* come through it: hand-edited, produced by an older version, or assembled
by something that skipped a check.

It re-validates rather than trusting provenance, because a payload file carries
no evidence of how it was made.

    buildsmith validate build/components/*.json
    buildsmith validate --dir build/

Exit status is 1 if anything fails. Nothing here touches a site.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from buildsmith.primitives import components as components_mod
from buildsmith.primitives import template as template_mod
from buildsmith.primitives import tokens as tokens_mod
from buildsmith.primitives.blocks import BlockError
from buildsmith.primitives.blocks import validate as validate_block

ROOT = Path(__file__).resolve().parents[2]

__all__ = ["validate_payload", "main"]


def _validate_component(payload: dict) -> list[str]:
    problems: list[str] = []
    component_id = payload.get("component_id") or payload.get("name")
    if not component_id:
        return ["no component_id — the record's name derives from it (TRAP-005)"]
    try:
        components_mod.slug_to_component_id(component_id)
    except BlockError as exc:
        problems.append(str(exc))

    block = payload.get("block")
    if not isinstance(block, dict) or not block:
        return problems + ["`block` must be a non-empty object (a component root is one block)"]

    try:
        validate_block(block)
    except BlockError as exc:
        problems.append(str(exc))
    try:
        components_mod.assert_colours_tokenised(block, path=component_id)
    except BlockError as exc:
        problems.append(str(exc))

    # Page shells match component interiors by blockId, so every node needs
    # one. A node without it is invisible to the TRAP-001 guards: Builder mints
    # a random id on save, no shell references it, and it renders on no
    # existing page. This is the last gate for payloads that skipped the
    # primitives — exactly the population likely to carry an id-less child.
    from buildsmith.primitives.blocks import assert_ids_assigned

    try:
        assert_ids_assigned(block)
    except BlockError as exc:
        problems.append(f"{exc} Page shells match on blockId (TRAP-001).")
    return problems


def _validate_page(payload: dict) -> list[str]:
    problems: list[str] = []
    blocks = payload.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        problems.append("`blocks` must be a non-empty list (a page holds a list, not one block)")
    else:
        for index, block in enumerate(blocks):
            try:
                validate_block(block, path=f"blocks[{index}]")
            except BlockError as exc:
                problems.append(str(exc))

    if payload.get("template_group") and not payload.get("is_template"):
        problems.append(
            "template_group is set but is_template is not. Builder's read-only guard and "
            "its fixture export both test the two together, so this combination does "
            "nothing and reads as though it should (TRAP-006)."
        )
    return problems


def _validate_token_plan(payload: dict) -> list[str]:
    problems: list[str] = []
    for index, operation in enumerate(payload.get("operations") or []):
        kind = operation.get("kind")
        where = f"operations[{index}]"
        if kind == "delete":
            problems.append(
                f"{where}: a delete operation. Deleting a Builder Variable orphans every "
                "var(--uuid) reference to it and nothing cascades (TRAP-007)."
            )
        elif kind != "mint" and not operation.get("uuid"):
            problems.append(f"{where}: '{kind}' needs the uuid of the record it changes")
        elif kind == "mint":
            record = operation.get("payload") or {}
            if "name" in record:
                problems.append(
                    f"{where}: a mint must not supply `name`. Builder assigns a uuid, and "
                    "upstream rewrites any non-uuid name on the next migrate (ADR-004)."
                )
            if record.get("type") not in tokens_mod.TOKEN_TYPES:
                problems.append(
                    f"{where}: type {record.get('type')!r} — only "
                    f"{sorted(tokens_mod.TOKEN_TYPES)} exist (TRAP-004)."
                )
    return problems


def validate_payload(payload: Any, *, source: str = "payload") -> list[str]:
    """Return a list of problems. Empty means it is safe to hand off."""
    if not isinstance(payload, dict):
        return [f"{source}: expected an object, got {type(payload).__name__}"]

    doctype = payload.get("doctype")
    if doctype == components_mod.DOCTYPE:
        problems = _validate_component(payload)
    elif doctype == template_mod.DOCTYPE:
        problems = _validate_page(payload)
    elif doctype == tokens_mod.DOCTYPE and "operations" in payload:
        problems = _validate_token_plan(payload)
    elif "operations" in payload:
        problems = _validate_token_plan(payload)
    else:
        return [
            f"{source}: unrecognised payload — expected a doctype of "
            f"'{components_mod.DOCTYPE}' or '{template_mod.DOCTYPE}', or a token plan with "
            "'operations'. Refusing to guess: an unvalidated payload that looks validated "
            "is worse than an obvious error."
        ]
    return [f"{source}: {p}" for p in problems]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("paths", nargs="*", help="payload JSON files")
    parser.add_argument("--dir", help="validate every .json under this directory")
    args = parser.parse_args(argv)

    paths = [Path(p) for p in args.paths]
    if args.dir:
        paths += sorted(Path(args.dir).rglob("*.json"))
    if not paths:
        parser.error("nothing to validate — pass files or --dir")

    problems: list[str] = []
    for path in paths:
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"{path}: could not read — {exc}")
            continue
        problems += validate_payload(payload, source=str(path))

    if problems:
        print(f"validate: {len(problems)} problem(s) in {len(paths)} payload(s)", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    print(f"validate: {len(paths)} payload(s) OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
