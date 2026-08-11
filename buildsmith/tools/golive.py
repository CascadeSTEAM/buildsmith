#!/usr/bin/env python3
"""Generate a go-live plan for one site. Choreography only — it executes nothing.

A static checklist goes stale and gets skimmed. This one is generated from the
site's actual build, so it names the real routes, the real component payloads,
and only the steps that this site actually needs — a site with no shipped
template group is not told to enable developer_mode, so the steps that *are*
listed keep their weight.

Both workflows produce a plan, from their own source of truth:

- a **theme** site (W2) is recomputed from `design/`, so a plan can never
  drift from the design inputs — but note it describes the *current* design,
  which is why the plan is regenerated rather than edited;
- a **replicate** site (W1) has no design inputs to recompute from, so its
  plan is generated from the emitted `build/` payloads themselves — the files
  that would actually ship. Validated first, same as handoff.

Every live step is marked with who performs it. Buildsmith emits artifacts; the
operations project performs actions (ADR-002), and a plan that blurs that is how
someone ends up running a DNS change from a design repo.

    buildsmith golive --site example
    buildsmith golive --site example --out sites/example/go-live.md

Nothing here touches a site.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from buildsmith.errors import EXIT_OK
from buildsmith.primitives.template import (
    Page,
    check_routes,
    prerequisites,
    requires_developer_mode,
    side_effects,
)
from buildsmith.tools import validate
from buildsmith.workflows.theme import BuildResult, build_site
from buildsmith.workflows.theme.build import BuildError

ROOT = Path(__file__).resolve().parents[2]

__all__ = ["generate", "main"]

OPS = "**operations project**"
HERE = "buildsmith"


def _workflow_for(site_dir: Path) -> str:
    """Which workflow built this site — the `workflow:` field of site.yml.

    Older sites without the field are inferred from what is on disk: a build
    directory with no design directory is a replicate by construction.
    """
    site_yml = site_dir / "site.yml"
    if site_yml.is_file():
        for line in site_yml.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("workflow:"):
                return line.split(":", 1)[1].strip().strip("'\"")
    if (site_dir / "build" / "pages").is_dir() and not (site_dir / "design").is_dir():
        return "replicate"
    return "theme"


def _page_from_record(record: dict) -> Page:
    """Rebuild the `Page` a `build/pages/*.json` payload came from.

    The emitted record is `Page.record()` plus `_client_scripts`, so every
    field maps back exactly. The name Builder assigned is not in a W1 payload
    (and cannot be predicted, TRAP-012), which is fine — the plan needs no name.
    """
    return Page(
        title=record.get("page_title") or record.get("route") or "untitled",
        route=record.get("route") or "",
        blocks=record.get("blocks") or [],
        template_group=record.get("template_group"),
        is_template=bool(record.get("is_template")),
        dynamic_route=bool(record.get("dynamic_route")),
        published=bool(record.get("published")),
        project_folder=record.get("project_folder"),
        favicon=record.get("favicon"),
        head_html=record.get("head_html") or "",
        scripts=record.get("_client_scripts") or [],
    )


def _result_from_build(site_dir: Path, *, site: str) -> BuildResult:
    """Reconstruct the build surface from emitted payloads — the W1 path.

    A replicate site has no `design/` to recompute from, and the emitted
    `build/` directory IS what would ship, so the plan is generated from the
    payloads themselves rather than from anything that could drift from them.
    """
    build = site_dir / "build"
    pages_dir = build / "pages"
    if not pages_dir.is_dir():
        raise BuildError(
            f"{build}/pages does not exist. This site has no emitted build; clone "
            f"or build it (buildsmith clone/build --site {site}) before planning a "
            "go-live."
        )

    # Same gate as handoff: never write a plan for payloads nobody checked.
    if validate.main(["--dir", str(build)]) != EXIT_OK:
        raise BuildError(
            f"the build payloads in {build} do not validate. A go-live plan must "
            "describe what actually ships, and unvalidated payloads are not "
            "allowed to ship (buildsmith validate)."
        )

    pages = [
        _page_from_record(json.loads(path.read_text()))
        for path in sorted(pages_dir.glob("*.json"))
    ]
    template = next((p for p in pages if p.is_template), None)
    ordinary = [p for p in pages if not p.is_template]
    return BuildResult(
        site=site,
        token_plan=None,
        components=[],
        template=template,
        pages=ordinary,
    )


def generate(site: str, *, root: Path | None = None) -> str:
    base = root or ROOT
    site_dir = base / "sites" / site
    workflow = _workflow_for(site_dir)
    if workflow == "replicate":
        result = _result_from_build(site_dir, site=site)
    else:
        result = build_site(site_dir, site=site)
    return _render(result, site, workflow)


def _render(result: BuildResult, site: str, workflow: str) -> str:
    pages = ([result.template] if result.template else []) + result.pages
    shipped = [p for p in pages if requires_developer_mode(p)]
    route_notes = check_routes(pages)

    out: list[str] = [
        f"# Go-live plan — `{site}`",
        "",
        "Generated by `buildsmith golive` from this site's actual build. Regenerate it",
        "rather than editing it; a hand-edited plan stops matching the payloads.",
        "",
        "**Buildsmith performs none of the live steps.** Each one below says who does.",
        "",
        "## What is being shipped",
        "",
        "| what | count |",
        "|---|---|",
    ]
    out += [f"| {k.replace('_', ' ')} | {v} |" for k, v in sorted(result.counts.items())]
    out.append(f"| workflow | {workflow} |")
    out.append("")

    if result.warnings:
        out += ["### Build warnings — resolve before going live", ""]
        out += [f"- {w}" for w in result.warnings]
        out.append("")

    needed = prerequisites(pages)
    if needed:
        out += ["## Prerequisites on the target site", "",
                "These are about the target, not the payloads, so no amount of validating",
                "the files will catch them. Each one was found by applying real payloads",
                "to a fresh site and watching them fail.", ""]
        out += [f"- [ ] {OPS}: {item}" for item in needed]
        out.append("")

    out += ["## Before anything", ""]
    out += [
        f"1. [ ] {OPS}: back up the site, and take a `Builder Snapshot`. Both.",
        f"2. [ ] {OPS}: confirm the scheduler and workers are running. `queue_action` "
        "with no worker locks documents permanently and the lock outlives the "
        "request (TRAP-009).",
        f"3. [ ] {OPS}: check `System Settings.time_zone` is set. Blank means "
        "timestamps are stored in IST, which has already caused a stale probe to "
        "be trusted (TRAP-008).",
        f"4. [ ] {HERE}: `buildsmith test` and `buildsmith validate --site {site}` are green.",
        "",
    ]

    out += ["## Tokens", ""]
    operations = len(result.token_plan) if result.token_plan else 0
    if operations:
        out += [
            f"1. [ ] {OPS}: apply `token-plan.json` — {operations} operation(s). "
            "It contains no deletes by construction; if you find yourself wanting "
            "one, stop and read TRAP-007.",
            f"2. [ ] {OPS}: read the token map back into `sites/{site}/"
            "tokens-applied.json`.",
            f"3. [ ] {HERE}: rebuild. Composition embeds each token's live value as "
            "the fallback in every reference, so components built before this are "
            "carrying stale literals.",
            "",
        ]
    else:
        if workflow == "replicate":
            out += [
                "Nothing to do — a W1 replica keeps its colours inline in block",
                "styles, not as Builder tokens. Tokenize it later with the W2 pass",
                "if you want tokens.",
                "",
            ]
        else:
            out += ["Nothing to do — the site's tokens already match the manifest.", ""]

    out += ["## Components", ""]
    if result.components:
        out.append(
            f"1. [ ] {HERE}: for each payload, a clean `buildsmith simulate` run against a "
            "**current** state export. Exit 1 means it would collapse pages "
            "that work today (TRAP-001); exit 2 means nothing was checked — "
            "re-read the export, and never treat it as a pass."
        )
        for index, component in enumerate(result.components, start=2):
            out.append(
                f"{index}. [ ] {OPS}: apply `components/{component.component_id}.json`"
                + (
                    "  — **needs a ComponentSyncer pass across every consuming page**, "
                    "or its new nodes render nowhere"
                    if component.meta.get("requires_component_sync")
                    else ""
                )
            )
        out.append("")
    else:
        out += ["_No components in this build._", ""]

    out += ["## Template and pages", ""]
    step = 1
    if shipped:
        for template in shipped:
            out.append(
                f"{step}. [ ] {OPS}: enable `developer_mode` — required to save "
                f"`{template.title}`, and it must be turned off again afterwards."
            )
            step += 1
            for effect in side_effects(template):
                out.append(f"     - {effect}")
    for item in pages:
        out.append(
            f"{step}. [ ] {OPS}: apply `{item.title}` at route `/{item.route}`"
            + ("  _(template)_" if item.is_template else "")
        )
        step += 1
    if shipped:
        out.append(f"{step}. [ ] {OPS}: disable `developer_mode` again.")
        step += 1
    out.append("")

    if route_notes:
        out += ["### Route shadowing — one page at a time", ""]
        out += [f"- {note}" for note in route_notes]
        out += [
            "",
            "Build the dynamic page and confirm each record renders **before** "
            "retiring the static one. Retiring first 404s live URLs; renaming breaks "
            "inbound links (TRAP-010).",
            "",
        ]

    out += ["## After applying, before looking", ""]
    out += [
        f"1. [ ] {OPS}: clear the website cache — `bench --site <site> clear-website-cache`.",
        "     `find_page_with_path` is redis-cached for an hour, so a replaced page leaves",
        "     the route pointing at a docname that no longer exists and **every visitor",
        "     gets a 403** until the cache expires. Found by browsing a freshly applied",
        "     replica and being told 'Not Permitted' (TRAP-015).",
        "",
    ]

    out += ["## Cutover", ""]
    out += [
        f"1. [ ] {OPS}: DNS and reverse proxy, via its own playbooks and roles.",
        f"2. [ ] {OPS}: certificates issued and serving.",
        "3. [ ] verify **every** route over the public hostname, not just locally:",
        "",
    ]
    out += [f"   - [ ] `/{item.route}` — {item.title}" for item in pages if not item.is_template]
    out += [
        "",
        f"4. [ ] {HERE}: `buildsmith journal append --site {site} --tool golive` and attach "
        "the rendered build log to the ticket.",
        "",
        "## If it goes wrong",
        "",
        "Restore the snapshot taken in step 1. That is what it is for, and it is why",
        "the first step is not optional.",
        "",
    ]

    return "\n".join(out).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--site", required=True)
    parser.add_argument("--out", help="write here instead of stdout")
    args = parser.parse_args(argv)

    plan = generate(args.site)
    if args.out:
        Path(args.out).write_text(plan)
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
