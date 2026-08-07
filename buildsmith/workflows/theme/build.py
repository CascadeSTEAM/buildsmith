"""Build a themed site from its design inputs.

The W2 pipeline, in order, because the order is not arbitrary:

1. **Tokens.** Diff the manifest against what is live and emit a plan. Nothing
   downstream can be composed correctly until the site's tokens match intent,
   because a reference embeds the live value as its fallback.
2. **Components.** Compose each spec, resolving `@token` references through the
   applied map, refusing literal colours.
3. **Template.** The mandatory `is_template=1` page. Emitted always.
4. **Pages.** Ordinary pages, which cannot be built without the template.

Inputs live in the site's private layer and are plain data:

    sites/<site>/design/tokens.json         the manifest (intent)
    sites/<site>/design/components/*.json   component specs
    sites/<site>/design/template.json       the template page spec
    sites/<site>/design/pages/*.json        ordinary page specs (optional)
    sites/<site>/tokens-applied.json        what is live (read back, optional)

Specs reference tokens by logical key with an `@` sigil — `"@brand-primary"` —
which resolves to `var(--<uuid>, <literal>)`. Specs therefore contain no uuids
and no colours, so they stay readable and survive a token being remapped.

Nothing here touches a site. It reads files and returns payloads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from buildsmith.primitives.blocks import BlockError
from buildsmith.primitives.components import Component, compose
from buildsmith.primitives.template import (
    Page,
    assert_template_emitted,
    check_routes,
    page,
    page_template,
    prerequisites,
)
from buildsmith.primitives.tokens import Applied, Manifest, Plan, TokenError, plan

__all__ = ["BuildError", "BuildResult", "build_site", "resolve_tokens"]

#: Marks a token reference inside a spec. A sigil rather than a bare key so a
#: literal string is never mistaken for one, and a typo'd key fails loudly
#: instead of silently rendering as text.
TOKEN_SIGIL = "@"


class BuildError(BlockError):
    """The site's design inputs cannot be built."""


def resolve_tokens(value: Any, applied: Applied, *, path: str = "") -> Any:
    """Replace `@key` with `var(--uuid, literal)` throughout a spec.

    Walks dicts, lists and strings. An unknown key raises rather than passing
    through: a spec that references a token nobody minted should not render as
    the literal text "@brand-primary" in a page's styles.
    """
    if isinstance(value, dict):
        return {k: resolve_tokens(v, applied, path=f"{path}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_tokens(v, applied, path=f"{path}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, str) and value.startswith(TOKEN_SIGIL):
        key = value[len(TOKEN_SIGIL):]
        try:
            return applied.ref(key)
        except TokenError as exc:
            raise BuildError(f"{path or 'spec'}: {exc}") from exc
    return value


@dataclass
class BuildResult:
    """Everything one build produced. Payloads, not actions."""

    site: str
    token_plan: Plan | None = None
    components: list[Component] = field(default_factory=list)
    template: Page | None = None
    pages: list[Page] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Records/settings the target site needs before these can be applied.
    prerequisites: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "token_operations": len(self.token_plan) if self.token_plan else 0,
            "components": len(self.components),
            "pages": len(self.pages),
            "templates": 1 if self.template else 0,
        }

    def write(self, out_dir: str | Path) -> list[Path]:
        """Emit the payloads as files for someone else to apply."""
        out = Path(out_dir)
        (out / "components").mkdir(parents=True, exist_ok=True)
        written: list[Path] = []

        if self.token_plan is not None:
            written.append(self.token_plan.write(out / "token-plan.json"))

        for component in self.components:
            path = out / "components" / f"{component.component_id}.json"
            path.write_text(json.dumps(component.record(), indent=2) + "\n")
            written.append(path)

        pages = ([self.template] if self.template else []) + self.pages
        if pages:
            (out / "pages").mkdir(parents=True, exist_ok=True)
            for item in pages:
                slug = (item.route or "home").replace("/", "_") or "home"
                path = out / "pages" / f"{slug}.json"
                path.write_text(json.dumps(item.record(), indent=2) + "\n")
                written.append(path)

        return written


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise BuildError(f"{path}: not valid JSON — {exc}") from exc


def build_site(site_dir: str | Path, *, site: str | None = None) -> BuildResult:
    """Build every payload for one site from its design inputs."""
    root = Path(site_dir)
    name = site or root.name
    design = root / "design"

    if not design.is_dir():
        raise BuildError(
            f"{design} does not exist. A themed site needs design inputs: at minimum "
            "design/tokens.json and design/template.json."
        )

    result = BuildResult(site=name)

    # --- 1. tokens ----------------------------------------------------------
    manifest_path = design / "tokens.json"
    if not manifest_path.exists():
        raise BuildError(f"{manifest_path} is missing — a themed site is defined by its tokens.")
    manifest = Manifest.from_dict(_load(manifest_path))

    applied_path = root / "tokens-applied.json"
    live = Applied.from_dict(_load(applied_path)) if applied_path.exists() else Applied()

    # The plan is always diffed against what is *actually* live, so a site with
    # no token map yet correctly plans to mint everything. Composition is a
    # separate question: it needs a uuid per token, and on a fresh site there is
    # none. Those two needs must not be served by the same object, or the plan
    # reports updates to tokens that do not exist.
    if applied_path.exists():
        applied = live
    else:
        applied = Applied.from_dict(
            {
                "tokens": {
                    key: {
                        "uuid": f"UNMINTED-{key}",
                        "value": token.value,
                        "variable_name": token.variable_name,
                    }
                    for key, token in manifest.tokens.items()
                }
            }
        )
        result.warnings.append(
            f"no {applied_path.name}: composing against placeholder uuids (UNMINTED-*). "
            "These payloads are a preview only — applying them would write references "
            "that resolve to nothing, leaving every value silently stuck on its literal "
            "fallback. Mint the tokens, read the map back, then rebuild."
        )

    result.token_plan = plan(manifest, live)
    if result.token_plan.orphans:
        result.warnings.append(
            f"{len(result.token_plan.orphans)} live token(s) are not in the manifest. "
            "They are left alone; retiring one is a decision to make after auditing its "
            "references (TRAP-007)."
        )

    # --- 2. components ------------------------------------------------------
    component_dir = design / "components"
    for spec_path in sorted(component_dir.glob("*.json")) if component_dir.is_dir() else []:
        spec = _load(spec_path)
        component_id = spec.get("component_id") or spec_path.stem
        root_block = spec.get("root")
        if not root_block:
            raise BuildError(f"{spec_path}: needs a 'root' block")

        resolved = resolve_tokens(root_block, applied, path=spec_path.name)
        # Only enforce the token discipline once the map is real; against
        # placeholder uuids the check would be theatre.
        result.components.append(
            compose(
                component_id=component_id,
                component_name=spec.get("name", component_id),
                root=resolved,
                applied=applied if applied_path.exists() else None,
                data_script=spec.get("data_script"),
            )
        )

    # --- 3. template (mandatory) -------------------------------------------
    template_path = design / "template.json"
    if not template_path.exists():
        raise BuildError(
            f"{template_path} is missing. Every site build emits a template — design "
            "tokens, reusable components, and a Builder Page with is_template=1. Skipping "
            "it makes later maintenance prohibitively expensive; there is no exception."
        )
    template_spec = _load(template_path)
    result.template = page_template(
        title=template_spec.get("title", f"{name} template"),
        route=template_spec.get("route", f"template/{name}"),
        blocks=resolve_tokens(template_spec["blocks"], applied, path="template.json"),
        template_group=template_spec.get("template_group"),
        project_folder=template_spec.get("project_folder"),
    )

    # --- 4. ordinary pages --------------------------------------------------
    page_dir = design / "pages"
    for spec_path in sorted(page_dir.glob("*.json")) if page_dir.is_dir() else []:
        spec = _load(spec_path)
        result.pages.append(
            page(
                title=spec.get("title", spec_path.stem),
                route=spec.get("route", spec_path.stem),
                blocks=resolve_tokens(spec["blocks"], applied, path=spec_path.name),
                template=result.template,
                published=spec.get("published", False),
                dynamic_route=spec.get("dynamic_route", False),
            )
        )

    everything = ([result.template] if result.template else []) + result.pages
    assert_template_emitted(everything)
    result.warnings.extend(check_routes(everything))
    result.prerequisites = prerequisites(everything)

    return result
