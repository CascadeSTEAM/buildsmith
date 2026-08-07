"""Repeaters, with every silent-failure mode enforced structurally.

TRAP-003. A repeater that is wrong does not raise, does not warn, and does not
look broken — it renders as an ordinary block showing one of everything. The
whole point of this module is that you cannot build one by hand, so you cannot
build one wrong.

Each rule below is checked against the pinned Builder in
`sandbox/trap-check.py`, so the ledger and the enforcement cannot drift apart.
Rule numbering matches `docs/traps.md`.
"""

from __future__ import annotations

from typing import Any

from buildsmith.primitives.blocks import BlockError, new_block

__all__ = [
    "ATTRIBUTE_PROPERTIES",
    "RepeaterError",
    "binding",
    "repeater",
    "validate_repeater",
]


class RepeaterError(BlockError):
    """A repeater is configured in a way that fails silently at render time."""


#: Properties that are HTML attributes rather than styles or content. Binding
#: one without `type: "attribute"` produces no attribute at all — verified at
#: the pin: an `img` with an unmarked `src` binding renders as `<img class="">`,
#: with the binding simply gone. Rule 4.
ATTRIBUTE_PROPERTIES = frozenset({"src", "href", "alt", "title", "target", "rel", "id"})


def binding(key: str, prop: str, *, type: str | None = None) -> dict:
    """Build one entry for `dynamicValues`.

    Infers `type: "attribute"` for properties that need it, and refuses an
    explicit type that contradicts the property — rule 4 is the kind of mistake
    that is easier to make deliberately than by accident.
    """
    if not key:
        raise RepeaterError("a binding needs a non-empty key")
    if not prop:
        raise RepeaterError("a binding needs a property to bind to")

    needs_attribute = prop in ATTRIBUTE_PROPERTIES
    if type is None:
        type = "attribute" if needs_attribute else "key"
    elif needs_attribute and type != "attribute":
        raise RepeaterError(
            f"'{prop}' is an HTML attribute, so its binding needs type='attribute', "
            f"not '{type}'. Without it the renderer emits no attribute at all and the "
            "binding vanishes with no error (TRAP-003 rule 4)."
        )

    return {"key": key, "property": prop, "type": type}


def repeater(
    *,
    data_key: dict | str,
    child: dict,
    element: str = "div",
    block_id: str | None = None,
    **block_kwargs: Any,
) -> dict:
    """Build a repeater container that cannot be silently wrong.

    `child` is the single block that repeats. Passing a list is refused rather
    than quietly truncated: at the pin, `render_repeater_children` reads
    `children[0]` and appends only that, so siblings are not rendered at all —
    not once, not ever (rule 2).
    """
    if isinstance(child, (list, tuple)):
        raise RepeaterError(
            f"a repeater takes exactly one child block, got {len(child)}. Only "
            "children[0] is rendered — later siblings are dropped outright, with no "
            "error (TRAP-003 rule 2). Wrap them in a single container block."
        )
    if not isinstance(child, dict):
        raise RepeaterError(f"child must be a block dict, got {type(child).__name__}")

    if isinstance(data_key, str):
        data_key = {"key": data_key, "comesFrom": "dataScript"}
    if not isinstance(data_key, dict) or not data_key.get("key"):
        raise RepeaterError(
            "a repeater needs a dataKey with a 'key'. Without it the block renders "
            "as an ordinary div — one item, no loop, no error (TRAP-003 rule 1)."
        )

    block = new_block(element, block_id=block_id, children=[child], **block_kwargs)
    block["isRepeaterBlock"] = True
    block["dataKey"] = dict(data_key)

    validate_repeater(block)
    return block


def validate_repeater(block: dict, *, path: str = "root") -> None:
    """Check a repeater against every rule that fails silently. Raises."""
    where = f"{path}: repeater"

    # --- rule 1: all three keys, or it is not a repeater at all --------------
    # At the pin this is literally one expression:
    #   bool(block.get("isRepeaterBlock") and block.get("children") and block.get("dataKey"))
    # There is no partial credit and no warning.
    missing = [
        name
        for name, present in (
            ("isRepeaterBlock", block.get("isRepeaterBlock")),
            ("children", block.get("children")),
            ("dataKey", block.get("dataKey")),
        )
        if not present
    ]
    if missing:
        raise RepeaterError(
            f"{where} is missing {missing}. All three are required together; without "
            "any one of them the renderer treats this as an ordinary block and shows a "
            "single item (TRAP-003 rule 1)."
        )

    # --- rule 2: exactly one child -------------------------------------------
    children = block["children"]
    if len(children) != 1:
        raise RepeaterError(
            f"{where} has {len(children)} children; only children[0] is rendered and "
            "the rest are dropped outright (TRAP-003 rule 2). Wrap them in one container."
        )
    child = children[0]

    # --- rule 3: the loop iterates dataKey.key -------------------------------
    # `property` on the *container* binds the container itself, not the
    # iteration. Setting it here is almost always a mix-up.
    data_key = block["dataKey"]
    if not isinstance(data_key, dict) or not data_key.get("key"):
        raise RepeaterError(f"{where} has a dataKey with no 'key' (TRAP-003 rule 3).")
    if data_key.get("property"):
        raise RepeaterError(
            f"{where} sets 'property' on its dataKey. That binds the container, not the "
            "iteration — the loop reads dataKey.key. Bind the child instead "
            "(TRAP-003 rule 3)."
        )

    # --- rule 4: attribute bindings must say so ------------------------------
    for node_label, node in (("container", block), ("child", child)):
        for dv in node.get("dynamicValues") or []:
            prop = dv.get("property")
            if prop in ATTRIBUTE_PROPERTIES and dv.get("type") != "attribute":
                raise RepeaterError(
                    f"{where} {node_label} binds '{prop}' without type='attribute'. The "
                    "renderer emits no attribute at all and the binding vanishes "
                    "(TRAP-003 rule 4). Build bindings with primitives.repeater.binding()."
                )

    # --- rule 5: no duplicate binding across dataKey and dynamicValues -------
    # Upstream fixed the resulting raw-Jinja leak before the current pin — it
    # now dedupes by (property, type). We still refuse duplicates: the emitter
    # should not depend on a downstream fix for correctness, and a pin that
    # moves backwards would reintroduce the leak.
    for node_label, node in (("container", block), ("child", child)):
        node_key = node.get("dataKey") or {}
        if not node_key.get("property"):
            continue
        signature = (node_key.get("property"), node_key.get("type"))
        for dv in node.get("dynamicValues") or []:
            if (dv.get("property"), dv.get("type")) == signature:
                raise RepeaterError(
                    f"{where} {node_label} declares the same binding "
                    f"{signature} in both dataKey and dynamicValues (TRAP-003 rule 5). "
                    "Harmless at the current pin, which dedupes; it leaked raw Jinja "
                    "before that. Declare it once."
                )

    # --- rule 6: visibilityCondition is not evaluated on the immediate child -
    # Confirmed at the pin: render_children() sets a visibility key on each
    # child, render_repeater_children() does not. The condition is accepted,
    # stored, and never consulted — so the block is always visible.
    if child.get("visibilityCondition"):
        raise RepeaterError(
            f"{where} sets visibilityCondition on its immediate child, where it is "
            "never evaluated — the block renders unconditionally (TRAP-003 rule 6). "
            "Move the condition one level down, or filter the data instead."
        )
