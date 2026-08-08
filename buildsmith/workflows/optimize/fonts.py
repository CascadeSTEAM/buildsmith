"""Font normalization — the reviewed-visible transform (ADR-009).

Builder's editor stores one font family per style, not a CSS stack; a stack
in `fontFamily` renders fine but breaks the editor's font picker — which is
the live site's state today. This transform reduces each accepted stack to
its primary family.

It is *not* part of the machine-invisible set, on principle: on any machine
where the primary font fails to load, dropping the fallbacks changes what
renders. So apply demands two proofs and one signature:

  - the loads assertion — every accepted family must be loaded by every page
    it is used on (Google Fonts link, @font-face, or @import), otherwise the
    reduction would render whatever the browser defaults to;
  - the rendering oracle (run it after) proves the sandbox drew the same
    pixels, because the webfonts do load there;
  - a human signs off the proposal file, same as tokenize — `status` flips
    to "accepted" by hand.

Shares its skeleton with tokenize: proposal file in, staleness guard,
tree-walked rewrite of style values only, blockId fingerprint unchanged.
"""
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path
from urllib.parse import unquote_plus

from buildsmith.errors import CouldNotCheck
from buildsmith.workflows.optimize.tokenize import (
    STYLE_KEYS,
    CannotProve,
    _refuse_stale_checkpoint,
    _select,
    block_ids,
    load_state,
    walk,
)

ROOT = Path(__file__).resolve().parents[3]


def primary_family(stack: str) -> str:
    """First family of a CSS font stack, unquoted and trimmed."""
    return stack.split(",")[0].strip().strip("'\"")


def mine_fonts(trees: dict[str, list[dict]]) -> dict[str, dict]:
    """{stack: {occurrences, where}} for every fontFamily style value."""
    found: dict[str, dict] = {}
    for label, roots in trees.items():
        for root in roots:
            for block in walk(root):
                for key in STYLE_KEYS:
                    styles = block.get(key)
                    if not isinstance(styles, dict):
                        continue
                    stack = styles.get("fontFamily")
                    if not isinstance(stack, str) or not stack.strip():
                        continue
                    entry = found.setdefault(
                        stack, {"occurrences": 0, "where": set()})
                    entry["occurrences"] += 1
                    entry["where"].add(label)
    return found


def proposal_path(site: str) -> Path:
    return ROOT / "sites" / site / "opt" / "proposals" / "fonts.json"


def mine(site: str, *, routes: list[str] | None = None) -> dict:
    """Mine stacks into the proposal file, preserving human edits on re-run."""
    pages, components = load_state(site)
    selected = _select(site, pages, components, routes)
    mined = mine_fonts(selected)

    path = proposal_path(site)
    prior = {}
    if path.exists():
        prior = {p["stack"]: p
                 for p in json.loads(path.read_text())["proposals"]}
    proposals = []
    for stack, info in sorted(mined.items(),
                              key=lambda kv: -kv[1]["occurrences"]):
        kept = prior.get(stack, {})
        already_single = "," not in stack
        proposals.append({
            "stack": stack,
            "primary": kept.get("primary", primary_family(stack)),
            "occurrences": info["occurrences"],
            "where": sorted(info["where"]),
            "status": kept.get(
                "status", "single" if already_single else "proposed"),
        })
    data = {
        "_meta": {
            "site": site,
            "note": "Reducing a stack to `primary` is a VISIBLE change on "
                    "machines where that font fails to load — a human flips "
                    "status to \"accepted\" with that understood. Entries "
                    "already single-family are marked \"single\" and never "
                    "rewritten.",
        },
        "proposals": proposals,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return data


def accepted_reductions(site: str) -> dict[str, str]:
    """{stack: primary} for accepted proposals. Refuses an empty primary."""
    path = proposal_path(site)
    if not path.exists():
        raise CouldNotCheck(f"no proposal file at {path} — mine first")
    data = json.loads(path.read_text())
    accepted = {p["stack"]: p["primary"].strip()
                for p in data["proposals"] if p["status"] == "accepted"}
    empty = [s for s, prim in accepted.items() if not prim]
    if empty:
        raise SystemExit(f"accepted with empty primary: {empty}")
    return accepted


def rewrite_fonts(roots: list[dict], reductions: dict[str, str]) -> int:
    """fontFamily values matching an accepted stack become its primary."""
    replaced = 0
    for root in roots:
        for block in walk(root):
            for key in STYLE_KEYS:
                styles = block.get(key)
                if not isinstance(styles, dict):
                    continue
                stack = styles.get("fontFamily")
                if isinstance(stack, str) and stack in reductions:
                    styles["fontFamily"] = reductions[stack]
                    replaced += 1
    return replaced


def check_loads(clone_url: str, routes: list[str], families: list[str],
                *, opener=None) -> list[str]:
    """Families not provably loaded on every route that must serve them.

    Looks for the family name inside font-loading constructs only: a Google
    Fonts URL (`family=Inter`), an `@font-face` block, or an `@import` of a
    font CSS. A bare mention in a style attribute does not count — that is
    the *use*, not the *load*.
    """
    open_fn = opener or urllib.request.urlopen
    problems: list[str] = []
    base = clone_url.rstrip("/")
    for route in routes:
        url = f"{base}/{route.lstrip('/')}".rstrip("/") or base
        request = urllib.request.Request(
            url, headers={"User-Agent": "buildsmith-optimize/0.1 (fonts)"})
        try:
            with open_fn(request, timeout=15) as response:
                html = response.read().decode("utf-8", "replace")
        except OSError as exc:
            raise CannotProve(f"cannot fetch {url}: {exc} — font loading "
                              "cannot be proved") from exc
        loaded = _loaded_families(html)
        for family in families:
            if family.strip().lower() not in loaded:
                problems.append(f"{route or '/'}: {family}")
    return problems


def _loaded_families(html: str) -> set[str]:
    """Family names this page actually LOADS, lowercased and decoded.

    Parsed as names, never matched as substrings — 'Inter' must not pass on
    the strength of a page that loads only 'Inter Tight'.
    """
    families: set[str] = set()
    for url in re.findall(r"fonts\.googleapis\.com/[^\"'>)]+", html):
        for name in re.findall(r"family=([^:&\"'>)]+)", url):
            families.add(unquote_plus(name).strip().lower())
    for face in re.findall(r"@font-face[^}]+}", html):
        for name in re.findall(r"font-family\s*:\s*['\"]?([^;'\"}]+)", face):
            families.add(name.strip().lower())
    return families


def apply(site: str, *, clone_url: str = "http://127.0.0.1:8000",
          target: str = "sandbox.localhost",
          routes: list[str] | None = None, runner=None) -> dict:
    """Apply accepted reductions; prove every family still loads."""
    from buildsmith.tools.sandbox import run_bench

    reductions = accepted_reductions(site)
    if not reductions:
        raise CouldNotCheck("no accepted proposals — nothing to apply")
    _refuse_stale_checkpoint(site, target=target)

    pages, components = load_state(site)
    selected = _select(site, pages, components, routes)
    if not selected:
        raise SystemExit(
            f"REFUSED: route filter {routes!r} selected no pages")

    out_dir = ROOT / "sites" / site / "opt" / "transforms" / "fonts"
    out_dir.mkdir(parents=True, exist_ok=True)
    updates: dict[str, dict] = {}
    total = 0
    for label, roots in selected.items():
        before = block_ids(roots)
        replaced = rewrite_fonts(roots, reductions)
        if block_ids(roots) != before:
            raise SystemExit(f"REFUSED: {label} block ids changed during a "
                             "style-only rewrite — this is a bug (TRAP-001)")
        if replaced:
            total += replaced
            kind, _, name = label.partition(":")
            updates[label] = {"kind": kind, "name": name, "blocks": roots}
            (out_dir / f"{kind}-{name}.json").write_text(json.dumps(
                {**updates[label], "replacements": replaced}, indent=2) + "\n")

    run = runner or run_bench
    if updates:
        # Ledger before mutation: a failed write-back must still show as an
        # applied-but-unproved gate. Library-level so no caller skips it.
        from buildsmith.workflows.optimize import gates

        gates.record_apply(site, "fonts", target=target)
        script = f"""
import frappe, json
frappe.init({json.dumps(target)}); frappe.connect()
frappe.flags.in_import = True
for item in json.loads({json.dumps(json.dumps(updates))}).values():
    if item['kind'] == 'page':
        doc = frappe.get_doc('Builder Page', item['name'])
        doc.blocks = json.dumps(item['blocks'])
    else:
        name = frappe.db.get_value('Builder Component',
                                   {{'component_id': item['name']}}, 'name')
        doc = frappe.get_doc('Builder Component', name)
        doc.block = json.dumps(item['blocks'][0] if len(item['blocks']) == 1
                               else item['blocks'])
    doc.save()
frappe.db.commit()
from frappe.website.utils import clear_website_cache
clear_website_cache()
print('applied', len(json.loads({json.dumps(json.dumps(updates))})))
"""
        run(script)

    # an accepted reduction that matched nothing is a decision that silently
    # did nothing — surfaced, not fatal (an idempotent re-run looks like this)
    present = set(mine_fonts(selected))
    unmatched = sorted(s for s in reductions if s not in present)

    # which routes must PROVE the load: every published page that was
    # rewritten, plus every published page that renders a rewritten
    # component — a component-only rewrite still changes what pages serve.
    rewritten_components = {u["name"] for u in updates.values()
                            if u["kind"] == "component"}
    page_routes: list[str] = []
    unservable: list[str] = []
    state = ROOT / "sites" / site / "opt" / "baseline" / "state"
    for path in sorted((state / "pages").glob("*.json")):
        record = json.loads(path.read_text())
        trees = pages.get(record["name"], [])
        uses_rewritten = any(
            b.get("extendedFromComponent") in rewritten_components
            for root in trees for b in walk(root)) if rewritten_components \
            else False
        if f"page:{record['name']}" in updates or uses_rewritten:
            if record.get("published"):
                page_routes.append(record.get("route") or "")
            else:
                unservable.append(record.get("route") or record["name"])

    families = sorted(set(reductions.values()))
    if updates and not page_routes:
        raise CannotProve(
            "the rewrite touched only unservable targets "
            f"(drafts/components: {sorted(updates)}) — no published route "
            "exists on which the load could be proved")
    unloaded = check_loads(clone_url, page_routes, families) \
        if page_routes else []

    return {"reductions": reductions, "replacements": total,
            "targets": sorted(updates), "unloaded": unloaded,
            "unmatched": unmatched,
            "coverage": ("partial" if unservable else "complete"),
            "unproved": unservable,
            "ok": not unloaded}
