"""Builder Pages, and the template every site build must emit.

The rule this module exists to enforce: **every site build emits a template.**
Design tokens, reusable components, and a `Builder Page` with `is_template=1`.
Skipping it is cheap now and makes maintenance prohibitively expensive later,
which is why `page()` will not let you build an ordinary page for a site that
has no template.

There are two kinds of template, they behave differently, and conflating them
is the mistake worth avoiding:

**A saved template** — `is_template=1` alone. What a user gets from "save as
template". Ungated, no side effects, invisible to the fixture sync.

**A shipped template group** — `is_template=1` *and* `template_group`. This is a
different animal:

- It can only be modified with **developer_mode on the live site**, and it is
  read-only in production (`BuilderPage.validate` throws otherwise).
- Saving one **writes files onto the server**: `on_update` calls
  `export_template_group()`, which exports the whole group to
  `<app>/builder/builder_templates/<group>/` plus assets under
  `www/builder_assets/<group>/`.
- Deleting one deletes its fixtures in developer mode, and throws in production.

That export is not a detail. It means enabling developer_mode and saving a
template page leaves committed-looking files inside an app directory on a live
host — which is exactly what a version audit of one deployment found sitting in
its Builder app, unexplained until this code was read.

Nothing here touches a site. These functions build payloads.

Verified against the pinned Builder commit (`sandbox/pins.env`).
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

from buildsmith.primitives.blocks import BlockError, validate

__all__ = [
    "DOCTYPE",
    "HOME_ROUTE",
    "prerequisites",
    "Page",
    "TemplateError",
    "assert_template_emitted",
    "check_routes",
    "page",
    "page_template",
    "requires_developer_mode",
    "side_effects",
]


class TemplateError(BlockError):
    """A page or template payload is malformed, or unsafe to apply."""


DOCTYPE = "Builder Page"

#: `template_group` becomes a directory name under `builder_templates/`, so it
#: has to survive a filesystem as well as a database.
_GROUP = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")

#: What a home page must actually be called. An **empty** route cannot be used:
#: `BuilderPage.set_default_values()` rewrites it to `pages/<name>`, so the page
#: lands at an unpredictable hash URL and `/` keeps serving the desk login. The
#: site's front door is `Website Settings.home_page`, which must name a real
#: route (TRAP-014). Found by browsing a replicated site and getting a login
#: screen where the homepage should have been.
HOME_ROUTE = "home"

#: A route with a leading slash is normalised by WebsiteGenerator, but whitespace
#: or capitals produce a page reachable at a URL nobody expects. Placeholder
#: segments (`:slug`, `<slug>`) are allowed because a dynamic route needs them.
_ROUTE = re.compile(r"^[a-z0-9:<][a-z0-9\-_/:<>.]*$")


@dataclass
class Page:
    """A `Builder Page` payload."""

    title: str
    route: str
    blocks: list[dict]
    #: Assigned by Builder and only known after the page exists. Set it when
    #: you have read it back; never invent one.
    name: str | None = None
    template_group: str | None = None
    is_template: bool = False
    dynamic_route: bool = False
    published: bool = False
    project_folder: str | None = None
    #: `/files/<name>` on the target. Without it Builder serves its own default
    #: and the clone wears somebody else's logo in the browser tab.
    favicon: str | None = None
    #: Page-level CSS that cannot be a block style — rules for elements a script
    #: creates at runtime, and id/descendant selectors.
    head_html: str = ""
    #: Page-local JavaScript, as Builder Client Script bodies. External and
    #: analytics-shaped scripts are never carried.
    scripts: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def record(self) -> dict[str, Any]:
        """The creation payload.

        `blocks` is a **list** of root blocks — unlike `Builder Component.block`,
        which is a single dict. The field is Long Text holding JSON; the applier
        serialises it.

        **`name` is deliberately never sent.** Unlike a component, a page's name
        cannot be chosen: `BuilderPage.autoname()` runs before the doctype's
        `field:page_name` rule and unconditionally assigns `page-<hash8>`, then
        `page_name` is force-synced to it. Verified at the pin — supplying
        `name`, `page_name`, or both is silently ignored in all three cases
        (TRAP-012). Sending it makes a payload look reproducible when it is not.
        """
        payload: dict[str, Any] = {
            "doctype": DOCTYPE,
            "page_title": self.title,
            "route": self.route,
            "blocks": self.blocks,
            "published": int(self.published),
            "dynamic_route": int(self.dynamic_route),
        }
        if self.is_template:
            payload["is_template"] = 1
        if self.template_group:
            payload["template_group"] = self.template_group
        if self.project_folder:
            payload["project_folder"] = self.project_folder
        if self.favicon:
            payload["favicon"] = self.favicon
        if self.head_html:
            payload["head_html"] = self.head_html
        return payload

    def update_payload(self) -> dict[str, Any]:
        """The payload for updating an existing page, which needs its name.

        The name has to come from the live site — it was assigned there and
        cannot be predicted. Raises rather than emitting a payload that would
        silently create a second page instead of updating the intended one.
        """
        if not self.name:
            raise TemplateError(
                f"{self.title!r}: updating a page needs the name Builder assigned it, "
                "which can only be read back from the site. Page names are "
                "`page-<hash8>` and are not chooseable (TRAP-012)."
            )
        payload = self.record()
        payload["name"] = self.name
        return payload

    @property
    def is_shipped_template(self) -> bool:
        """Both flags set — the gated, fixture-exporting kind."""
        return bool(self.is_template and self.template_group)


def requires_developer_mode(page_obj: Page) -> bool:
    """Whether applying this payload needs developer_mode on the **live site**.

    Only a shipped template group does. `is_template` on its own does not — the
    guard in `BuilderPage.validate` tests both fields together, and the fixture
    sync deliberately leaves user-saved templates alone.
    """
    return page_obj.is_shipped_template


def side_effects(page_obj: Page) -> list[str]:
    """What applying this payload does beyond writing a row.

    Returned rather than logged so a go-live plan can print it, and so nobody
    discovers the filesystem writes by finding unexplained files later.
    """
    if not page_obj.is_shipped_template:
        return []
    group = page_obj.template_group
    return [
        "developer_mode must be enabled on the live site, or the save is refused "
        "(PermissionError). Enable it, apply, then disable it again (TRAP-006).",
        f"on_update exports the whole '{group}' group to "
        f"<app>/builder/builder_templates/{group}/ on the server — every page, "
        f"component, and variable in it, not just this page.",
        f"group assets are written to <app>/builder/www/builder_assets/{group}/.",
        "in production the page becomes read-only; deleting it throws unless "
        "developer_mode is on, in which case it deletes the fixtures too.",
    ]


def prerequisites(pages: list[Page]) -> list[str]:
    """Records and settings that must exist on the target *before* applying.

    Payload validation cannot catch these: they are about the target site, not
    the payload. Both were found by applying real payloads to a fresh site and
    watching them fail.
    """
    needed: list[str] = []

    folders = sorted({p.project_folder for p in pages if p.project_folder})
    for folder in folders:
        needed.append(
            f"`Builder Project Folder` named {folder!r} must exist. `project_folder` is a "
            "Link field, so applying a page before the folder exists fails with "
            "LinkValidationError."
        )

    home = next((p for p in pages if p.route == HOME_ROUTE), None)
    if home:
        needed.append(
            f"`Website Settings.home_page` must be set to {HOME_ROUTE!r}, or `/` keeps "
            f"serving the desk login however many pages are published (TRAP-014)."
        )
    return needed


def _validate_common(title: str, route: str, blocks: list[dict]) -> list[dict]:
    if not title or not title.strip():
        raise TemplateError("a page needs a non-empty title")

    if not isinstance(blocks, list):
        raise TemplateError(
            f"blocks must be a list of root blocks, got {type(blocks).__name__}. "
            "A page holds a list; only Builder Component.block is a single dict."
        )
    if not blocks:
        raise TemplateError("a page needs at least one root block")

    normalised = route.strip().strip("/")
    if not normalised:
        raise TemplateError(
            "a page needs a route, and an empty one is not 'the home page'. Builder "
            f"rewrites an empty route to `pages/<name>`, so the page ends up at an "
            f"unpredictable hash URL and `/` still serves the desk login. Use "
            f"{HOME_ROUTE!r} and set `Website Settings.home_page` to it (TRAP-014)."
        )
    if normalised and not _ROUTE.match(normalised):
        raise TemplateError(
            f"route {route!r} should be lower-case, slash-separated, no spaces "
            "(e.g. 'about' or 'services/design')."
        )
    # An empty segment survives the pattern above but produces a URL nobody
    # means: `posts//x` is not `posts/x`, and only one of them is reachable.
    if normalised and any(segment == "" for segment in normalised.split("/")):
        raise TemplateError(
            f"route {route!r} has an empty path segment. Collapse the double slash — "
            "it does not normalise away, it makes a different and unreachable URL."
        )

    blocks = copy.deepcopy(blocks)
    for index, block in enumerate(blocks):
        validate(block, path=f"blocks[{index}]")
    return blocks


def page_template(
    *,
    title: str,
    route: str,
    blocks: list[dict],
    template_group: str | None = None,
    name: str | None = None,
    project_folder: str | None = None,
    favicon: str | None = None,
    head_html: str = "",
    scripts: list[str] | None = None,
) -> Page:
    """Build the template page every site build must emit.

    Pass `template_group` only when you mean a *shipped* group — it is what
    turns on the developer_mode gate and the fixture export. Leave it off for a
    plain saved template, which has neither.
    """
    blocks = _validate_common(title, route, blocks)

    if template_group is not None:
        if not _GROUP.match(template_group):
            raise TemplateError(
                f"template_group {template_group!r} becomes a directory under "
                "builder_templates/, so it must be a lower-case slug "
                "(e.g. 'acme-marketing')."
            )

    return Page(
        title=title,
        route=route.strip().strip("/"),
        blocks=blocks,
        name=name,
        template_group=template_group,
        is_template=True,
        project_folder=project_folder,
        favicon=favicon,
        head_html=head_html,
        scripts=list(scripts or []),
    )


def page(
    *,
    title: str,
    route: str,
    blocks: list[dict],
    template: Page | None = None,
    name: str | None = None,
    published: bool = False,
    dynamic_route: bool = False,
    project_folder: str | None = None,
    favicon: str | None = None,
    head_html: str = "",
    scripts: list[str] | None = None,
) -> Page:
    """Build an ordinary page.

    `template` is required, and is the mechanism behind the no-exceptions rule
    that every site build emits one. It costs a caller nothing to pass and makes
    the omission impossible to reach by accident rather than merely discouraged.
    """
    if template is None:
        raise TemplateError(
            "every site build must emit a template — design tokens, reusable "
            "components, and a Builder Page with is_template=1 — before its ordinary "
            "pages. Build it with page_template() and pass it here. Skipping the "
            "template makes later maintenance prohibitively expensive; there is no "
            "exception to this rule."
        )
    if not template.is_template:
        raise TemplateError(
            f"the page passed as `template` ({template.title!r}) does not have "
            "is_template set, so it is an ordinary page, not a template."
        )

    blocks = _validate_common(title, route, blocks)

    return Page(
        title=title,
        route=route.strip().strip("/"),
        blocks=blocks,
        name=name,
        published=published,
        dynamic_route=dynamic_route,
        project_folder=project_folder or template.project_folder,
        favicon=favicon or template.favicon,
        head_html=head_html,
        scripts=list(scripts or []),
    )


def assert_template_emitted(pages: list[Page]) -> list[Page]:
    """Check a site build actually produced a template, and return the templates.

    The last line of defence for the rule, for a build that assembles its pages
    some other way.

    Several templates are fine and normal: a template *group* is a set of pages
    — landing, contact, and so on — sharing one set of components and variables,
    and `export_template_group` exports every page in it. What is not fine is a
    build whose shipped templates disagree about which group they belong to,
    since the group is the unit that gets exported.
    """
    templates = [p for p in pages if p.is_template]
    if not templates:
        raise TemplateError(
            f"this build emitted {len(pages)} page(s) and no template. Every site build "
            "emits one: design tokens, reusable components, and a Builder Page with "
            "is_template=1. No exceptions."
        )

    groups = {p.template_group for p in templates if p.template_group}
    if len(groups) > 1:
        raise TemplateError(
            f"this build's templates span {len(groups)} template groups ({sorted(groups)}). "
            "The group is the unit Builder exports, so pages in different groups are "
            "different deliverables — build them separately."
        )
    return templates


def check_routes(pages: list[Page]) -> list[str]:
    """Refuse colliding routes; report shadowing. TRAP-010.

    Two things, and only one of them is an error:

    **Duplicates are an error.** `find_page_with_path` resolves a route with
    `order_by published_at desc, creation desc` — so two published pages on the
    same route do not conflict, they race, and the most recently published one
    silently wins. Nothing reports the loser.

    **Shadowing is reported, not refused.** A static route always resolves
    before the dynamic matcher runs, so `posts/hello` wins over `posts/<slug>`.
    That is usually a deliberate transitional state: TRAP-010's rule is to build
    the template and verify each record renders *before* retiring the legacy
    page, one at a time. Retiring first 404s live URLs.
    """
    seen: dict[str, Page] = {}
    for candidate in pages:
        clash = seen.get(candidate.route)
        if clash:
            raise TemplateError(
                f"two pages share the route {candidate.route!r}: {clash.title!r} and "
                f"{candidate.title!r}. Builder resolves a route by most-recently-published, "
                "so this does not error at render time — one page silently wins and the "
                "other becomes unreachable."
            )
        seen[candidate.route] = candidate

    dynamic = [p for p in pages if p.dynamic_route]
    notes = []
    for static in (p for p in pages if not p.dynamic_route):
        segments = static.route.split("/")
        for pattern in dynamic:
            parts = pattern.route.split("/")
            if len(parts) != len(segments):
                continue
            if all(_is_placeholder(a) or a == b
                   for a, b in zip(parts, segments, strict=True)):
                notes.append(
                    f"static route {static.route!r} ({static.title!r}) shadows dynamic "
                    f"{pattern.route!r} ({pattern.title!r}) — the static page wins. Verify "
                    "the dynamic page renders this record before retiring the static one "
                    "(TRAP-010)."
                )
    return notes


def _is_placeholder(segment: str) -> bool:
    return segment.startswith((":", "<")) or segment.endswith(">")
