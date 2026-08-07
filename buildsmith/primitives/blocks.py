"""Block builders, and the invariants that make a block tree safe to write back.

A Builder block is a plain dict. That is convenient and dangerous in equal
measure: the renderer reads a fixed set of keys and **silently ignores every
other one**, so a misspelled key is not an error, it is a feature that quietly
does nothing. Half this module exists to turn those silences into exceptions.

Key names are Builder's, so they are camelCase here even though the surrounding
Python is not. Renaming them would only add a translation layer to get wrong.

Verified against the pinned Builder commit (`sandbox/pins.env`).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from typing import Any

__all__ = [
    "BlockError",
    "RENDERED_KEYS",
    "assert_ids_assigned",
    "assert_ids_preserved",
    "assign_ids",
    "block_ids",
    "new_block",
    "restyle",
    "validate",
    "walk",
    "walk_paths",
]


class BlockError(ValueError):
    """A block tree violates an invariant we know breaks at render time."""


#: Every key the pinned renderer actually reads off a block. Extracted from the
#: renderer rather than from the frontend's type definitions, because the
#: renderer is what decides whether a page comes out right. A key outside this
#: set is not rejected by Builder — it is ignored, which is worse.
RENDERED_KEYS = frozenset(
    {
        "attributes",
        "baseStyles",
        "blockClientScript",
        "blockId",
        "children",
        "classes",
        "clientScript",
        "componentVersion",
        "customAttributes",
        "dataKey",
        "dynamicValues",
        "element",
        "extendedFromComponent",
        "innerHTML",
        "isRepeaterBlock",
        "mobileStyles",
        "originalElement",
        "props",
        "rawStyles",
        # Written by Builder onto a page's override shells; `extend_block()`
        # matches a shell to its component child on
        # (shell.blockId, shell.referenceBlockId) — see docs/traps.md TRAP-001.
        # Authoring never writes it (new_block has no parameter for it), but a
        # tree read back from a live site is full of them, and validate()
        # refusing what the renderer reads would make every read-back invalid.
        "referenceBlockId",
        "tabletStyles",
        "visibilityCondition",
    }
)

#: Style buckets, in the order Builder resolves them.
STYLE_KEYS = ("baseStyles", "mobileStyles", "tabletStyles", "rawStyles")


def new_block(
    element: str,
    *,
    block_id: str | None = None,
    children: list[dict] | None = None,
    classes: list[str] | None = None,
    inner_html: str | None = None,
    attributes: dict[str, Any] | None = None,
    custom_attributes: dict[str, Any] | None = None,
    base_styles: dict[str, Any] | None = None,
    mobile_styles: dict[str, Any] | None = None,
    tablet_styles: dict[str, Any] | None = None,
    raw_styles: dict[str, Any] | None = None,
    dynamic_values: list[dict] | None = None,
    visibility_condition: dict | str | None = None,
    extends_component: str | None = None,
    component_version: str | None = None,
) -> dict:
    """Build one block.

    Only non-empty values are written, so the emitted JSON stays close to what
    Builder itself produces and diffs stay readable.

    `block_id` is optional here and assigned later by :func:`assign_ids`. It is
    deliberately not random — see that function.
    """
    if not element or not isinstance(element, str):
        raise BlockError(f"element must be a non-empty string, got {element!r}")

    block: dict[str, Any] = {"element": element}

    if block_id is not None:
        block["blockId"] = block_id
    if children:
        block["children"] = list(children)
    if classes:
        block["classes"] = list(classes)
    if inner_html is not None:
        block["innerHTML"] = inner_html
    if attributes:
        block["attributes"] = dict(attributes)
    if custom_attributes:
        block["customAttributes"] = dict(custom_attributes)
    if base_styles:
        block["baseStyles"] = dict(base_styles)
    if mobile_styles:
        block["mobileStyles"] = dict(mobile_styles)
    if tablet_styles:
        block["tabletStyles"] = dict(tablet_styles)
    if raw_styles:
        block["rawStyles"] = dict(raw_styles)
    if dynamic_values:
        block["dynamicValues"] = [dict(dv) for dv in dynamic_values]
    if visibility_condition is not None:
        block["visibilityCondition"] = visibility_condition
    if extends_component is not None:
        block["extendedFromComponent"] = extends_component
    if component_version is not None:
        block["componentVersion"] = component_version

    return block


def walk(block: dict) -> Iterator[dict]:
    """Yield every block in the tree, depth first, parents before children."""
    yield block
    for child in block.get("children") or []:
        if isinstance(child, dict):
            yield from walk(child)


def walk_paths(block: dict, *, path: str | None = None) -> Iterator[tuple[dict, str]]:
    """Yield every block with its structural path, parents before children.

    The path formula is shared with `assign_ids`, which derives blockIds from
    it — the two must never drift, which is why this is one function.
    """
    path = path if path is not None else block.get("element", "?")
    yield block, path
    for index, child in enumerate(block.get("children") or []):
        if isinstance(child, dict):
            yield from walk_paths(
                child, path=f"{path}/{child.get('element', '?')}[{index}]"
            )


def block_ids(block: dict) -> set[str]:
    """Every `blockId` present in the tree."""
    return {b["blockId"] for b in walk(block) if b.get("blockId")}


def assert_ids_assigned(block: dict) -> None:
    """Raise if any node in the tree lacks a `blockId`.

    `block_ids()` skips id-less nodes, so every guard built on it runs blind on
    them: an id-less node is neither a preserved id nor a detectable addition.
    Builder mints a random id on save, no page shell references it, and the
    node renders in the editor and nowhere else (TRAP-001's mirror image).
    A caller comparing trees by id must refuse such a tree rather than reason
    about only the parts it can see.
    """
    missing = [path for node, path in walk_paths(block) if not node.get("blockId")]
    if missing:
        raise BlockError(
            f"{len(missing)} block(s) carry no blockId: {missing[:5]}"
            f"{'...' if len(missing) > 5 else ''}. Read the tree back from the "
            "site, or run assign_ids() first — it keeps existing ids and fills "
            "the gaps deterministically."
        )


def assign_ids(block: dict, *, seed: str, overwrite: bool = False) -> dict:
    """Assign deterministic `blockId`s in place, and return the tree.

    Builder generates random ids. We must not, for two reasons:

    1. **Diffs.** Random ids change on every emit, so a regenerated payload
       looks entirely rewritten and review becomes impossible.
    2. **TRAP-001.** Pages reference a component's interior by `blockId`. Ids
       derived from position mean re-emitting an unchanged tree reproduces the
       same ids, so the shells still match instead of collapsing.

    The id is a hash of `seed` plus the block's structural path, so it is stable
    across runs and machines but distinct per position. Existing ids are kept
    unless `overwrite=True` — an id read back from a live site is authoritative
    and must survive a round trip through our tooling.
    """

    for node, path in walk_paths(block):
        if overwrite or not node.get("blockId"):
            digest = hashlib.sha256(f"{seed}\x00{path}".encode()).hexdigest()
            node["blockId"] = digest[:16]
    return block


def assert_ids_preserved(before: dict, after: dict) -> None:
    """Raise if `after` drops any `blockId` that `before` had. TRAP-001.

    This is the check that would have caught the most expensive near-miss on
    record — a freshly composed component tree, written over an in-use one,
    which would have collapsed the header and footer across every page that
    referenced it. Pages match a component's interior by `blockId`; an id that
    disappears takes the page's shell content with it.

    Note the asymmetry: gaining ids is fine (the tree grew), losing them is not.
    """
    lost = block_ids(before) - block_ids(after)
    if lost:
        raise BlockError(
            f"{len(lost)} blockId(s) present before are missing after: "
            f"{sorted(lost)[:5]}{'...' if len(lost) > 5 else ''}. "
            "Pages reference component interiors by blockId — dropping one collapses "
            "that node to element=None on every consuming page (TRAP-001). Restyle in "
            "place, or run ComponentSyncer across every consumer in the same operation."
        )


def restyle(
    block: dict,
    *,
    base: dict | None = None,
    mobile: dict | None = None,
    tablet: dict | None = None,
    raw: dict | None = None,
) -> dict:
    """Merge style changes into a block in place, preserving its identity.

    The safe way to change an in-use component: the tree keeps its shape and its
    ids, so nothing downstream has to be resynced.
    """
    for key, incoming in (
        ("baseStyles", base),
        ("mobileStyles", mobile),
        ("tabletStyles", tablet),
        ("rawStyles", raw),
    ):
        if incoming:
            block.setdefault(key, {}).update(incoming)
    return block


def validate(block: dict, *, path: str = "root") -> None:
    """Validate a block tree structurally. Raises :class:`BlockError`.

    Deliberately strict about unknown keys. The renderer ignores them, so a
    typo like `baseStyle` costs an afternoon; here it costs an exception.
    """
    if not isinstance(block, dict):
        raise BlockError(f"{path}: expected a dict, got {type(block).__name__}")

    unknown = set(block) - RENDERED_KEYS
    if unknown:
        raise BlockError(
            f"{path}: unknown key(s) {sorted(unknown)}. The renderer silently ignores "
            f"keys it does not know, so this would fail invisibly. Known keys: "
            f"{sorted(RENDERED_KEYS)}"
        )

    if not block.get("element") and not block.get("extendedFromComponent"):
        raise BlockError(
            f"{path}: a block needs an 'element', unless it extends a component."
        )

    for key in STYLE_KEYS:
        if key in block and not isinstance(block[key], dict):
            raise BlockError(f"{path}: {key} must be a dict, got {type(block[key]).__name__}")

    children = block.get("children")
    if children is not None:
        if not isinstance(children, list):
            raise BlockError(f"{path}: children must be a list, got {type(children).__name__}")
        for index, child in enumerate(children):
            validate(child, path=f"{path}/{block.get('element', '?')}[{index}]")

    # A repeater has extra rules that are entirely its own; keeping them in one
    # place means there is a single answer to "is this repeater correct?".
    if block.get("isRepeaterBlock"):
        from buildsmith.primitives.repeater import validate_repeater

        validate_repeater(block, path=path)
