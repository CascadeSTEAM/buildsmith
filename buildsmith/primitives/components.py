"""Builder Components: composing them, and revising one without breaking pages.

A component is not embedded in the pages that use it. Each page carries a
mirror of **empty override shells**, and the renderer rebuilds the visible tree
by matching those shells against the component's children at render time. That
one fact generates most of the rules here.

The matching loop, from the pinned Builder's `extend_block()`, iterates the
*page's* shells:

    for overridden_child in overridden_children:        # the page's shells
        component_child = first child whose blockId is in
            (overridden_child.blockId, overridden_child.referenceBlockId)
        if component_child: merge the two
        else:               keep the bare shell        # <- the collapse

A shell that matches nothing is emitted as-is, and a shell is what
`reset_block_styles()` left behind: `element=None`, no innerHTML, no styles. So
re-issuing a component's blockIds does not "reset" those pages, it renders them
as a tree of nothing. The same loop means the reverse is also true: a child
added to a component never appears on an existing page, because no shell
references it.

Neither self-heals. `BuilderComponent.on_update` clears caches and mints a
version; it does **not** call `sync_component()`.

Nothing here touches a site. These functions build payloads.

Verified against the pinned Builder commit (`sandbox/pins.env`).
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

from buildsmith.primitives.blocks import (
    BlockError,
    assert_ids_assigned,
    assert_ids_preserved,
    assign_ids,
    block_ids,
    validate,
    walk,
)
from buildsmith.primitives.tokens import Applied, Manifest, validate_styles

__all__ = [
    "COLOUR_PROPERTIES",
    "DOCTYPE",
    "Component",
    "ComponentError",
    "assert_additions_acknowledged",
    "assert_colours_tokenised",
    "assert_content_preserved",
    "compose",
    "revise",
    "slug_to_component_id",
]


class ComponentError(BlockError):
    """A component payload would damage the pages that use it."""


DOCTYPE = "Builder Component"

#: Component ids may be readable, and this is *not* a contradiction of the rule
#: that token names must not be (see `tokens.DOCTYPE_NAMING`). The two doctypes
#: are treated oppositely upstream. `refactor_builder_variables` rewrites every
#: non-uuid *variable* name to a uuid; the component equivalent,
#: `set_component_id`, does the reverse — it copies `name` into `component_id`,
#: preserving whatever the name already was. Nothing upstream randomises a
#: component id, so a deterministic one is stable across migrations.
#:
#: `autoname` is `field:component_id`, so `name == component_id` and
#: `before_insert` only generates a hash when the field is empty. Supplying our
#: own is supported, and makes payloads reproducible and diffable.
DOCTYPE_NAMING = "component_id, which becomes name — ours to choose, and stable"

_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: CSS properties that can carry a colour. Kept explicit rather than inferred:
#: the point is to catch a literal that *should* have been a token. The
#: shorthands are here too — `border: 1px solid #fff` is where literals
#: actually live on real sites, and a value with no colour in it (`border:
#: 1px solid`) passes because nothing colour-shaped is found, not because the
#: property was exempt.
COLOUR_PROPERTIES: frozenset[str] = frozenset(
    {
        "accentColor",
        "backgroundColor",
        "borderBottomColor",
        "borderColor",
        "borderLeftColor",
        "borderRightColor",
        "borderTopColor",
        "caretColor",
        "color",
        "columnRuleColor",
        "fill",
        "outlineColor",
        "stroke",
        "textDecorationColor",
        # shorthands and compound values
        "background",
        "backgroundImage",
        "border",
        "borderBottom",
        "borderLeft",
        "borderRight",
        "borderTop",
        "boxShadow",
        "columnRule",
        "outline",
        "textDecoration",
        "textShadow",
    }
)

#: Colour-shaped functional notation, CSS Color 4 included. `color-mix` before
#: `color` so the alternation cannot half-match it.
_LITERAL_COLOUR = re.compile(
    r"(#[0-9a-fA-F]{3,8}\b"
    r"|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color-mix|color)\s*\()"
)

#: The CSS named colours (Color Module Level 4). `transparent`,
#: `currentColor`, and the inheritance keywords are deliberately absent —
#: they are semantics, not palette entries, and a token cannot express them.
_NAMED_COLOURS: frozenset[str] = frozenset(
    """aliceblue antiquewhite aqua aquamarine azure beige bisque black
    blanchedalmond blue blueviolet brown burlywood cadetblue chartreuse
    chocolate coral cornflowerblue cornsilk crimson cyan darkblue darkcyan
    darkgoldenrod darkgray darkgreen darkgrey darkkhaki darkmagenta
    darkolivegreen darkorange darkorchid darkred darksalmon darkseagreen
    darkslateblue darkslategray darkslategrey darkturquoise darkviolet
    deeppink deepskyblue dimgray dimgrey dodgerblue firebrick floralwhite
    forestgreen fuchsia gainsboro ghostwhite gold goldenrod gray green
    greenyellow grey honeydew hotpink indianred indigo ivory khaki lavender
    lavenderblush lawngreen lemonchiffon lightblue lightcoral lightcyan
    lightgoldenrodyellow lightgray lightgreen lightgrey lightpink lightsalmon
    lightseagreen lightskyblue lightslategray lightslategrey lightsteelblue
    lightyellow lime limegreen linen magenta maroon mediumaquamarine
    mediumblue mediumorchid mediumpurple mediumseagreen mediumslateblue
    mediumspringgreen mediumturquoise mediumvioletred midnightblue mintcream
    mistyrose moccasin navajowhite navy oldlace olive olivedrab orange
    orangered orchid palegoldenrod palegreen paleturquoise palevioletred
    papayawhip peachpuff peru pink plum powderblue purple rebeccapurple red
    rosybrown royalblue saddlebrown salmon sandybrown seagreen seashell
    sienna silver skyblue slateblue slategray slategrey snow springgreen
    steelblue tan teal thistle tomato turquoise violet wheat white
    whitesmoke yellow yellowgreen""".split()
)

_NAMED_COLOUR = re.compile(
    r"\b(?:" + "|".join(sorted(_NAMED_COLOURS)) + r")\b", re.IGNORECASE
)

#: Value segments that are exempt from the literal-colour scan: the whole
#: point of `var(--uuid, literal)` is that its fallback literal is sanctioned,
#: and `url(white.png)` is a filename, not a palette entry.
_EXEMPT_SEGMENT = re.compile(r"\b(?:var|url)\s*\(", re.IGNORECASE)


def _outside_refs(value: str) -> str:
    """The parts of a CSS value not inside `var(...)` or `url(...)`.

    Parens nest — `var(--u, rgb(0,0,0))` is one reference — so this counts
    depth instead of regexing to the first `)`. What survives is exactly the
    text the literal-colour scan is entitled to judge: checking the whole
    value would either bless everything near a `var()` (the old behaviour —
    `0 1px var(--u,#000), 0 2px #fff` sailed through on the first clause) or
    condemn the sanctioned fallback inside it.
    """
    kept: list[str] = []
    i, n = 0, len(value)
    while i < n:
        match = _EXEMPT_SEGMENT.search(value, i)
        if not match:
            kept.append(value[i:])
            break
        kept.append(value[i:match.start()])
        depth, j = 1, match.end()
        while j < n and depth:
            if value[j] == "(":
                depth += 1
            elif value[j] == ")":
                depth -= 1
            j += 1
        i = j
    return " ".join(kept)


def slug_to_component_id(slug: str) -> str:
    """Validate a component id. Lower-case, hyphen-separated, no surprises."""
    if not slug or not _SLUG.match(slug):
        raise ComponentError(
            f"component id {slug!r} must be a lower-case hyphenated slug "
            "(e.g. 'site-header'). It becomes the record's `name`, so it wants to "
            "stay readable and stable."
        )
    return slug


def assert_colours_tokenised(block: dict, *, path: str = "root") -> None:
    """Refuse a literal colour where a token reference belongs.

    A component with a hardcoded colour is invisible to the design system: it
    will not follow a palette change and will not respond to dark mode, and
    nothing reports it. The discipline is zero literal colours — every one is
    `var(--uuid, literal)`, where the literal survives only as the fallback.
    """
    for index, node in enumerate(walk(block)):
        where = f"{path}[{index}]:{node.get('element', '?')}"
        for bucket in ("baseStyles", "mobileStyles", "tabletStyles", "rawStyles"):
            for prop, value in (node.get(bucket) or {}).items():
                if prop not in COLOUR_PROPERTIES or not isinstance(value, str):
                    continue
                # Only what lies outside var()/url() is judged: a var()'s
                # fallback literal is the discipline working, not a breach —
                # but a second, un-tokenised colour beside it still fails.
                bare = _outside_refs(value)
                if _LITERAL_COLOUR.search(bare) or _NAMED_COLOUR.search(bare):
                    raise ComponentError(
                        f"{where}.{bucket}.{prop} = {value!r} holds a literal colour. "
                        "Use tokens.Applied.ref() so it becomes var(--uuid, literal) — "
                        "the literal stays as the fallback, but the token drives the "
                        "value."
                    )


def assert_content_preserved(before: dict, after: dict) -> None:
    """Refuse a revision that silently drops content. TRAP-002.

    A composed component is a structural skeleton and carries no content of its
    own, so swapping one in over a populated tree drops nav links, addresses,
    everything — with no error. Compared per blockId, because that is the only
    stable identity a node has across a revision.

    Covers `innerHTML` and the `href`/`src` attributes, since a nav's content is
    as much its links as its labels.
    """

    def content_by_id(tree: dict) -> dict[str, str]:
        found = {}
        for node in walk(tree):
            block_id = node.get("blockId")
            if not block_id:
                continue
            attributes = {**(node.get("attributes") or {}), **(node.get("customAttributes") or {})}
            carried = [node.get("innerHTML")] + [attributes.get(a) for a in ("href", "src")]
            values = [str(v) for v in carried if v]
            if values:
                found[block_id] = " | ".join(values)
        return found

    was, now = content_by_id(before), content_by_id(after)
    lost = sorted(key for key in was if key not in now)
    if lost:
        sample = ", ".join(f"{key}={was[key][:30]!r}" for key in lost[:3])
        raise ComponentError(
            f"{len(lost)} block(s) carried content before and carry none after: {sample}"
            f"{'...' if len(lost) > 3 else ''}. A composed component is a structural "
            "skeleton — it carries no content, so this would drop what is on the page "
            "with no error (TRAP-002). Compose from a source that includes the content, "
            "or read the existing content out first."
        )


def assert_additions_acknowledged(before: dict, after: dict) -> list[str]:
    """Report blockIds new in `after`, and refuse them unless acknowledged.

    The mirror image of the collapse, and easier to miss because nothing looks
    broken. `extend_block()` iterates the *page's* shells, so a child added to a
    component is matched by no shell on any existing page and is simply never
    rendered there. It will look perfect in the Builder editor and be absent in
    production.

    The fix is a `ComponentSyncer` pass over every consuming page, which creates
    the missing shells — and that is an action against a live site, so it cannot
    happen here. This function's job is to make sure nobody discovers the need
    for it afterwards.

    An `after` tree with id-less nodes is refused outright: such a node is not
    in `block_ids(after)`, so it would pass this diff unseen — and it can never
    be listed for the ComponentSyncer pass, so acknowledging cannot cover it.
    """
    try:
        assert_ids_assigned(after)
    except BlockError as exc:
        raise ComponentError(str(exc)) from exc

    added = sorted(block_ids(after) - block_ids(before))
    if added:
        raise ComponentError(
            f"{len(added)} new block(s) in this revision: {added[:5]}"
            f"{'...' if len(added) > 5 else ''}. Existing pages carry no shell "
            "referencing them, so they render in the editor and are absent on every "
            "page already using this component — with no error. Pass "
            "allow_additions=True once you have arranged a ComponentSyncer pass across "
            "every consuming page in the same operation."
        )
    return added


@dataclass
class Component:
    """A Builder Component payload."""

    component_id: str
    component_name: str
    block: dict
    data_script: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def record(self) -> dict[str, Any]:
        """The creation payload.

        `component_id` is `unique=1` and `read_only=1`, and `autoname` derives
        `name` from it — so it is set once, here, and never changed afterwards.
        Renaming the record while leaving `component_id` behind is what makes
        the two diverge, and `clear_page_cache()` matches on `component_id`
        while blocks reference it too (TRAP-005).
        """
        payload: dict[str, Any] = {
            "doctype": DOCTYPE,
            "component_id": self.component_id,
            "component_name": self.component_name,
            "block": self.block,
        }
        if self.data_script:
            payload["component_data_script"] = self.data_script
        return payload

    def update_payload(self) -> dict[str, Any]:
        """The revision payload: everything except identity.

        `component_id` is deliberately absent. Sending it on an update is how a
        rename sneaks in, and a component whose `name` and `component_id` differ
        breaks cache invalidation silently (TRAP-005).
        """
        payload: dict[str, Any] = {
            "doctype": DOCTYPE,
            "name": self.component_id,
            "component_name": self.component_name,
            "block": self.block,
        }
        if self.data_script is not None:
            payload["component_data_script"] = self.data_script
        return payload


def _check_token_pair(applied: Applied | None, manifest: Manifest | None) -> None:
    """Run the staleness check, or refuse a half-passed pair.

    `applied` alone is meaningful (colour discipline without a staleness
    check). `manifest` alone is not — it enables nothing, and the caller who
    passed it believes both the colour check and the staleness check are on.
    A silently ignored safety argument is worse than an error.
    """
    if manifest is not None and applied is None:
        raise ComponentError(
            "manifest= was passed without applied=. The manifest is only ever "
            "checked against an applied map, so alone it enables no check at all — "
            "pass applied= (with manifest= for the staleness check), or neither."
        )
    if applied is not None and manifest is not None:
        applied.assert_in_sync(manifest)


def compose(
    *,
    component_id: str,
    component_name: str,
    root: dict,
    applied: Applied | None = None,
    data_script: str | None = None,
    manifest: Manifest | None = None,
) -> Component:
    """Build a new component from a freshly composed tree.

    Ids are assigned deterministically from `component_id`, so recomposing an
    unchanged tree reproduces the same ids — which keeps diffs readable and,
    more importantly, keeps page shells matching.

    Pass `applied` to require that every colour is a token reference. Pass
    `manifest` alongside it and the applied map is checked for staleness first,
    since composing against a lagging map bakes yesterday's colours in as
    fallbacks.
    """
    slug_to_component_id(component_id)

    _check_token_pair(applied, manifest)

    # assign_ids mutates, and a caller reusing its own tree afterwards should not
    # silently find our ids on it.
    root = copy.deepcopy(root)
    validate(root)
    assign_ids(root, seed=f"component:{component_id}")

    for node in walk(root):
        for bucket in ("baseStyles", "mobileStyles", "tabletStyles", "rawStyles"):
            if node.get(bucket):
                validate_styles(node[bucket], path=f"{node.get('element', '?')}.{bucket}")

    if applied is not None:
        assert_colours_tokenised(root, path=component_id)

    return Component(
        component_id=component_id,
        component_name=component_name,
        block=root,
        data_script=data_script,
    )


def revise(
    previous: Component | dict,
    new_root: dict,
    *,
    allow_additions: bool = False,
    applied: Applied | None = None,
    manifest: Manifest | None = None,
) -> Component:
    """Revise an existing component, refusing the revisions that break pages.

    `previous` must be the component **as it is live** — read back, not
    reconstructed. Its blockIds are what every page's shells point at, and this
    is the check that the most expensive near-miss on record would have failed:
    a freshly composed tree written over an in-use component, which would have
    rendered the header and footer as `element=None` across all 13 pages.

    `applied`/`manifest` carry the same meaning as in :func:`compose`, and they
    matter *more* here: revision is the sanctioned way to change an in-use
    component's styles, which makes it exactly the path where a literal colour
    sneaks in. Pass `applied` whenever the site runs on tokens. The colour
    check covers the whole revised tree, not just the delta — deliberately:
    the discipline is zero literals, so a pre-existing one surfacing here is
    cleanup this revision should carry, not damage to wave through.
    """
    if isinstance(previous, Component):
        prev_id, prev_name, prev_block = (
            previous.component_id,
            previous.component_name,
            previous.block,
        )
        prev_script = previous.data_script
    else:
        prev_id = previous.get("component_id") or previous.get("name")
        prev_name = previous.get("component_name", prev_id)
        prev_block = previous.get("block") or {}
        prev_script = previous.get("component_data_script")

    if not prev_id:
        raise ComponentError(
            "the previous component has no component_id, so there is nothing to revise "
            "against. Read it back from the site rather than reconstructing it."
        )
    if not prev_block:
        raise ComponentError(
            f"{prev_id}: the previous component has an empty block tree. Without it there "
            "is no way to tell whether this revision preserves the ids that every page's "
            "shells point at (TRAP-001)."
        )

    _check_token_pair(applied, manifest)

    validate(new_root)

    # The same style discipline compose() applies — a revision is not a lesser
    # write, it lands on a component pages already use (TRAP-004).
    for node in walk(new_root):
        for bucket in ("baseStyles", "mobileStyles", "tabletStyles", "rawStyles"):
            if node.get(bucket):
                validate_styles(node[bucket], path=f"{node.get('element', '?')}.{bucket}")

    if applied is not None:
        assert_colours_tokenised(new_root, path=prev_id)

    # An id-less node is invisible to every id-based check below, and it cannot
    # appear in requires_component_sync, so no ComponentSyncer pass will ever
    # create its shells. Refuse before the guards run blind — with the seed the
    # generic block-layer message cannot know.
    try:
        assert_ids_assigned(new_root)
    except BlockError as exc:
        raise ComponentError(
            f"{prev_id}: {exc} For this component the seed is "
            f'"component:{prev_id}".'
        ) from exc

    # assert_ids_preserved belongs to the block layer and raises BlockError.
    # Re-raised as ComponentError so this layer has one exception type: a caller
    # catching ComponentError must not miss the collapse, which is the single
    # most damaging thing revise() exists to prevent.
    try:
        assert_ids_preserved(prev_block, new_root)
    except BlockError as exc:
        raise ComponentError(f"{prev_id}: {exc}") from exc

    assert_content_preserved(prev_block, new_root)

    added: list[str] = []
    if allow_additions:
        added = sorted(block_ids(new_root) - block_ids(prev_block))
    else:
        assert_additions_acknowledged(prev_block, new_root)

    return Component(
        component_id=prev_id,
        component_name=prev_name,
        block=new_root,
        data_script=prev_script,
        # Carried so the emitted payload can say so out loud: these nodes need a
        # ComponentSyncer pass across every consuming page or they render nowhere.
        meta={"requires_component_sync": added} if added else {},
    )
