"""Reproduce known traps inside the sandbox — the faithfulness check.

A sandbox that merely *runs* proves nothing. This proves it fails the same way
production does, on the two traps whose failure modes are entirely silent:

**TRAP-001** — a page carries empty override shells, not the component itself.
`extend_block()` matches each shell against the component's children, and a
shell that matches nothing is emitted exactly as `reset_block_styles()` left it:
`element=None`, no content. So re-issuing a component's blockIds renders those
pages as a tree of nothing, and the mirror case — a child *added* to a component
— renders nowhere at all, because no existing shell references it.

**TRAP-003** — a repeater missing any one of its required keys degrades into an
ordinary block. The observable is a Jinja `{% for %}` loop in the rendered HTML:
present when the repeater is whole, silently absent when it is not.

These are the behaviours `primitives/components.py` and `primitives/repeater.py`
enforce against. If a check here fails, the enforcement is modelled on a Builder
that no longer exists.

Run it through `buildsmith check traps`, which feeds this file to the bench's
python inside the container.
"""

import os
import sys

import frappe

SITE = os.environ.get("SANDBOX_SITE", "sandbox.localhost")

frappe.init(site=SITE)
frappe.connect()

from builder.builder.doctype.builder_page.builder_page import (  # noqa: E402
    extend_block,
    get_block_html,
)

CHILD = {"element": "p", "innerHTML": "row"}
SIBLING = {"element": "span", "innerHTML": "sibling"}


def repeater(*, data_key=True, children=(CHILD,)):
    """A repeater container, optionally missing one of its required keys."""
    block = {
        "element": "div",
        "isRepeaterBlock": True,
        "children": [dict(c) for c in children],
    }
    if data_key:
        block["dataKey"] = {"key": "items", "type": "Object"}
    return block


def html_of(block):
    return get_block_html([block])[0]


results = []


def check(label, condition, detail=""):
    results.append((label, bool(condition), detail))
    print(f"  {'PASS' if condition else 'FAIL'}  {label}")
    if not condition and detail:
        print(f"        {detail}")


print("TRAP-001 — a component shell that matches nothing collapses\n")

# A page does not embed a component. It carries override shells, and
# `extend_block()` rebuilds the visible tree by matching each shell's
# blockId/referenceBlockId against the component's children. A shell that
# matches nothing is emitted exactly as reset_block_styles() left it.


def component_tree(child_id):
    return {
        "element": "header",
        "blockId": "root",
        "children": [{"element": "nav", "blockId": child_id, "innerHTML": "REAL NAV"}],
    }


def page_shell(reference_id):
    """A page's mirror of the component: empty shells, keyed by reference."""
    return {
        "blockId": "shell-1",
        "referenceBlockId": "root",
        "extendedFromComponent": "c1",
        "children": [
            {
                "blockId": "shell-2",
                "referenceBlockId": reference_id,
                "element": None,
                "innerHTML": None,
                "baseStyles": {},
                "children": [],
            }
        ],
    }


matched = extend_block(component_tree("nav-1"), page_shell("nav-1"))["children"][0]
check(
    "a shell whose reference matches renders the component's content",
    matched.get("element") == "nav" and matched.get("innerHTML") == "REAL NAV",
    f"got element={matched.get('element')!r} innerHTML={matched.get('innerHTML')!r}",
)

# Re-issuing the component's blockIds — what composing a fresh tree does.
collapsed = extend_block(component_tree("nav-REISSUED"), page_shell("nav-1"))["children"][0]
check(
    "re-issuing a blockId collapses that node to element=None",
    collapsed.get("element") is None and collapsed.get("innerHTML") is None,
    f"got element={collapsed.get('element')!r} innerHTML={collapsed.get('innerHTML')!r}",
)
check(
    "...and the content is simply absent from the rendered page",
    "REAL NAV" not in get_block_html([collapsed])[0],
    "the content survived — the collapse model in primitives/components.py is wrong",
)

# The mirror image: a child added to a component is matched by no shell, so it
# never renders on a page that already exists.
grown = component_tree("nav-1")
grown["children"].append({"element": "button", "blockId": "added", "innerHTML": "NEW"})
grown_html = get_block_html([extend_block(grown, page_shell("nav-1"))])[0]
check(
    "a newly added component child renders nowhere on an existing page",
    "NEW" not in grown_html,
    "the addition rendered — no ComponentSyncer pass would be needed after all",
)

print()
print("TRAP-003 — repeaters degrade silently\n")

# --- rule 1: isRepeaterBlock AND children AND dataKey -----------------------
good = html_of(repeater())
check(
    "a complete repeater emits a Jinja loop",
    "{% for" in good and "{% endfor %}" in good,
    f"rendered: {good[:200]}",
)

no_key = html_of(repeater(data_key=False))
check(
    "dropping dataKey silently removes the loop",
    "{% for" not in no_key,
    f"rendered: {no_key[:200]}",
)
check(
    "...and the block still renders, so nothing looks wrong",
    "row" in no_key,
    "the child vanished entirely — the trap would at least be visible",
)

no_children = repeater()
no_children["children"] = []
empty = html_of(no_children)
check(
    "an empty children list silently removes the loop",
    "{% for" not in empty,
)

not_flagged = repeater()
del not_flagged["isRepeaterBlock"]
unflagged = html_of(not_flagged)
check(
    "dropping isRepeaterBlock silently removes the loop",
    "{% for" not in unflagged,
)

# --- rule 2: only children[0] repeats ---------------------------------------
two = html_of(repeater(children=(CHILD, SIBLING)))
check(
    "only children[0] is rendered — later siblings are dropped outright",
    "row" in two and "sibling" not in two,
    f"rendered: {two[:300]}",
)

# --- rule 4: attribute bindings need type="attribute" ------------------------
unmarked = html_of({"element": "img", "dynamicValues": [{"key": "u", "property": "src"}]})
marked = html_of(
    {"element": "img", "dynamicValues": [{"key": "u", "property": "src", "type": "attribute"}]}
)
check(
    "a src binding without type='attribute' emits no attribute at all",
    "src=" not in unmarked,
    f"rendered: {unmarked[:200]}",
)
check(
    "...and with it, the attribute appears",
    "src=" in marked,
    f"rendered: {marked[:200]}",
)

# --- rule 5: the duplicate-binding leak, fixed upstream before this pin ------
# Kept as a check rather than deleted: if the pin ever moves backwards, this
# turns the silent return of the leak into a failing test.
duplicated = html_of(
    {
        "element": "p",
        "innerHTML": "x",
        "dataKey": {"key": "title", "type": "key", "property": "innerHTML"},
        "dynamicValues": [{"key": "title", "type": "key", "property": "innerHTML"}],
    }
)
check(
    "a binding declared twice no longer leaks raw Jinja (fixed upstream at this pin)",
    "else '{{" not in duplicated,
    f"the nested-placeholder leak is back: {duplicated[:240]}",
)

# --- rule 6: visibilityCondition is ignored on a repeater's immediate child --
VIS = {"element": "p", "innerHTML": "row", "visibilityCondition": {"key": "show"}}
in_repeater = html_of(repeater(children=(VIS,)))
in_normal = html_of({"element": "div", "children": [dict(VIS)]})
check(
    "visibilityCondition is honoured on an ordinary child",
    "{% if" in in_normal,
    "the control case failed — the next check proves nothing",
)
check(
    "...and silently ignored on a repeater's immediate child",
    "{% if" not in in_repeater,
    f"rendered: {in_repeater[:240]}",
)

failed = [label for label, ok, _ in results if not ok]
print()
if failed:
    print(f"SANDBOX IS NOT FAITHFUL — {len(failed)} of {len(results)} checks failed.")
    print("The pinned Builder does not behave the way the trap ledger records.")
    print("Either the pin is wrong or docs/traps.md is stale. Resolve before")
    print("trusting anything else this sandbox tells you.")
    sys.exit(1)

print(f"Sandbox reproduces TRAP-001 and TRAP-003 — {len(results)}/{len(results)} checks.")
print(f"Builder: {frappe.get_attr('builder.__version__')}")
sys.exit(0)
