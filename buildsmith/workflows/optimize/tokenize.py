"""Tokenize — mine literal colours into design tokens, then rewrite by proof.

Two subcommands, deliberately split so a human sits between them:

    buildsmith optimize tokenize --site <s>            # mine -> proposal file
    buildsmith optimize tokenize --site <s> --apply    # accepted -> records + rewrite

The proposal file is the persisted decision artifact (ADR-009): each entry
carries a mined colour, its occurrence count, a suggested name, and a
`status` a human flips to "accepted". Apply consumes only accepted entries.

Rewrites touch style *values* only — blocks are walked as trees, never
string-replaced, because a hex literal can legitimately live in innerHTML or
an SVG fill, and those are content, not style. Colour matching is
case-insensitive: the live export carries lowercase hex, the sandbox serves
it back uppercase.

The resolution assertion is not optional. `var(--uuid, literal)` renders the
fallback whether or not the token exists, so after apply the served variables
stylesheet must contain every referenced UUID — otherwise the token layer is
dead weight that every gate would wave through (TRAP-013's shape).
"""
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

from buildsmith.errors import CouldNotCheck

ROOT = Path(__file__).resolve().parents[3]

#: style dict keys a Builder block may carry; the only places colours are
#: rewritten
STYLE_KEYS = ("baseStyles", "mobileStyles", "tabletStyles", "rawStyles")


class CannotProve(CouldNotCheck):
    """A proof step could not RUN (network down, nothing checkable).

    A subclass of :class:`buildsmith.errors.CouldNotCheck`, kept as a name
    because 'cannot prove' is the right word at a proof site; the CLI's one
    handler maps both to exit 2."""

_HEX = re.compile(
    r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?\b|#[0-9a-fA-F]{3,4}\b")

#: properties whose values are colour-bearing; anything else keeps literals
#: (a hex-looking string inside e.g. `content` must not be touched)
_COLOUR_PROPS = re.compile(
    r"color|background|border|outline|fill|stroke|shadow|caret|decoration",
    re.IGNORECASE)


def norm(colour: str) -> str:
    """Canonical lowercase 6/8-digit form; #abc -> #aabbcc, #abcd -> #aabbccdd."""
    colour = colour.lower()
    if len(colour) in (4, 5):
        colour = "#" + "".join(c * 2 for c in colour[1:])
    return colour


def walk(block: dict):
    yield block
    for child in block.get("children") or []:
        yield from walk(child)


def _iter_style_items(block: dict):
    for key in STYLE_KEYS:
        styles = block.get(key)
        if isinstance(styles, dict):
            yield styles


def mine_colours(trees: dict[str, list[dict]]) -> dict[str, dict]:
    """{normalized colour: {occurrences, where}} across every style value.

    `trees` maps a label (route or component id) to its parsed block roots.
    """
    found: dict[str, dict] = {}
    for label, roots in trees.items():
        for root in roots:
            for block in walk(root):
                for styles in _iter_style_items(block):
                    for prop, value in styles.items():
                        if not isinstance(value, str):
                            continue
                        if not _COLOUR_PROPS.search(prop):
                            continue
                        for hit in _HEX.findall(value):
                            entry = found.setdefault(
                                norm(hit), {"occurrences": 0, "where": set()})
                            entry["occurrences"] += 1
                            entry["where"].add(label)
    return found


def build_proposals(mined: dict[str, dict], *, site: str,
                    existing: dict | None = None) -> dict:
    """Proposal file content. Re-mining preserves human edits in `existing`
    (matched by colour value) — a re-run must never wipe a decision."""
    prior = {}
    if existing:
        prior = {p["value"]: p for p in existing.get("proposals", [])}
    proposals = []
    for value, info in sorted(mined.items(),
                              key=lambda kv: -kv[1]["occurrences"]):
        kept = prior.get(value, {})
        proposals.append({
            "value": value,
            "occurrences": info["occurrences"],
            "where": sorted(info["where"]),
            "name": kept.get("name", ""),
            "status": kept.get("status", "proposed"),
        })
    return {
        "_meta": {
            "site": site,
            "note": "Set `name` and flip `status` to \"accepted\" to include "
                    "a colour in the apply pass. Apply consumes accepted "
                    "entries only; this file is the decision record.",
        },
        "proposals": proposals,
    }


def rewrite_tree(roots: list[dict], mapping: dict[str, str]) -> int:
    """Rewrite style values in place: literal -> var(--uuid, literal).

    `mapping` is {normalized colour: uuid}. Returns replacements made.
    Values already containing var() references are left alone — rewriting a
    fallback would nest references.
    """
    replaced = 0

    def substitute(value: str) -> str:
        nonlocal replaced

        def _sub(match: re.Match) -> str:
            nonlocal replaced
            uuid = mapping.get(norm(match.group(0)))
            if not uuid:
                return match.group(0)
            replaced += 1
            return f"var(--{uuid}, {norm(match.group(0))})"

        return _HEX.sub(_sub, value)

    for root in roots:
        for block in walk(root):
            for styles in _iter_style_items(block):
                for prop, value in list(styles.items()):
                    if (isinstance(value, str) and "var(" not in value
                            and _COLOUR_PROPS.search(prop)):
                        styles[prop] = substitute(value)
    return replaced


# -- I/O and application ------------------------------------------------------

def proposal_path(site: str) -> Path:
    return ROOT / "sites" / site / "opt" / "proposals" / "tokens.json"


def load_state(site: str) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """(pages, components) block trees from the baseline checkpoint."""
    state = ROOT / "sites" / site / "opt" / "baseline" / "state"
    if not state.exists():
        raise CouldNotCheck(f"no baseline checkpoint at {state} — run "
                            "`buildsmith optimize baseline` first")
    pages: dict[str, list[dict]] = {}
    for path in sorted((state / "pages").glob("*.json")):
        record = json.loads(path.read_text())
        blocks = record.get("blocks")
        tree = json.loads(blocks) if isinstance(blocks, str) else blocks or []
        pages[record["name"]] = [tree] if isinstance(tree, dict) else tree
    components: dict[str, list[dict]] = {}
    for path in sorted((state / "components").glob("*.json")):
        record = json.loads(path.read_text())
        raw = record.get("block")
        tree = json.loads(raw) if isinstance(raw, str) else raw or []
        components[record["component_id"]] = (
            [tree] if isinstance(tree, dict) else tree)
    return pages, components


def mine(site: str, *, routes: list[str] | None = None) -> dict:
    """Mine and (re)write the proposal file. Returns its content."""
    pages, components = load_state(site)
    selected = _select(site, pages, components, routes)
    mined = mine_colours(selected)
    path = proposal_path(site)
    existing = json.loads(path.read_text()) if path.exists() else None
    proposals = build_proposals(mined, site=site, existing=existing)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(proposals, indent=2) + "\n")
    return proposals


def _select(site: str, pages: dict, components: dict,
            routes: list[str] | None) -> dict[str, list[dict]]:
    """Pages (by route filter, when given) plus every component their blocks
    extend — a page referencing a component with literal colours is not
    tokenized until the component is too."""
    state = ROOT / "sites" / site / "opt" / "baseline" / "state"
    chosen: dict[str, list[dict]] = {}
    used_components: set[str] = set()
    for path in sorted((state / "pages").glob("*.json")):
        record = json.loads(path.read_text())
        if routes is not None and (record.get("route") or "") not in routes:
            continue
        trees = pages[record["name"]]
        chosen[f"page:{record['name']}"] = trees
        for root in trees:
            for block in walk(root):
                ref = block.get("extendedFromComponent")
                if ref:
                    used_components.add(ref)
    for cid in sorted(used_components):
        if cid in components:
            chosen[f"component:{cid}"] = components[cid]
    return chosen


def accepted_mapping(site: str) -> dict[str, dict]:
    """{colour: proposal} for accepted, named proposals. Refuses ambiguity."""
    path = proposal_path(site)
    if not path.exists():
        raise CouldNotCheck(f"no proposal file at {path} — mine first")
    data = json.loads(path.read_text())
    accepted = [p for p in data["proposals"] if p["status"] == "accepted"]
    unnamed = [p["value"] for p in accepted if not p["name"]]
    if unnamed:
        raise SystemExit(f"accepted but unnamed: {unnamed} — a token needs "
                         "a human-given name before it becomes a record")
    names = [p["name"] for p in accepted]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise SystemExit(f"duplicate token names: {sorted(dupes)}")
    values = [p["value"] for p in accepted]
    dup_values = {v for v in values if values.count(v) > 1}
    if dup_values:
        # a dict keyed by value would silently keep one and drop the other —
        # losing a human decision without a word
        raise SystemExit(f"duplicate accepted values: {sorted(dup_values)} — "
                         "one colour, one token")
    return {p["value"]: p for p in accepted}


def block_ids(roots: list[dict]) -> list[str]:
    """Every blockId in tree order — the structural fingerprint a style-only
    rewrite must leave untouched (TRAP-001)."""
    return [str(b.get("blockId")) for root in roots for b in walk(root)]


def ensure_variables(site: str, accepted: dict[str, dict],
                     *, target: str = "sandbox.localhost",
                     runner=None) -> dict[str, str]:
    """One site-owned Builder Variable per accepted colour; {colour: uuid}.

    A proposal that already carries a `uuid` (from a previous apply) updates
    that record and nothing else. A proposal without one must mint a NEW
    record — if its `variable_name` already exists on the site, that is a
    collision with a variable this tool does not own, and the apply REFUSES:
    silently adopting a foreign variable entangles the site's palette with
    records something else controls, and overwriting its value repoints
    every existing reference to it (TRAP-007's blast radius). The human
    picks a different name; the tool never picks for them.

    Values are stored uppercase, matching what Builder's own editor writes.
    Never deletes anything (no delete operation exists here).
    """
    from buildsmith.tools.sandbox import run_bench

    wanted = [{"name": p["name"], "value": value.upper(),
               "uuid": p.get("uuid", "")} for value, p in accepted.items()]
    script = f"""
import frappe, json
frappe.init({json.dumps(target)}); frappe.connect()
frappe.flags.in_import = True
out, collisions = {{}}, []
for item in json.loads({json.dumps(json.dumps(wanted))}):
    if item['uuid']:
        doc = frappe.get_doc('Builder Variable', item['uuid'])
        if doc.variable_name != item['name']:
            # the uuid no longer points at the record we minted — updating
            # it would mutate a foreign variable
            collisions.append(item['name'])
            continue
        if doc.value != item['value']:
            doc.value = item['value']
            doc.save()
    else:
        existing = frappe.db.get_value('Builder Variable',
                                       {{'variable_name': item['name']}}, 'name')
        if existing:
            collisions.append(item['name'])
            continue
        doc = frappe.get_doc({{'doctype': 'Builder Variable',
                              'variable_name': item['name'],
                              'type': 'Color', 'value': item['value']}})
        doc.insert()
    out[item['name']] = doc.name
frappe.db.commit()
print(json.dumps({{'minted': out, 'collisions': collisions}}))
"""
    run = runner or run_bench
    result = json.loads(run(script).strip().splitlines()[-1])
    if result["collisions"]:
        raise SystemExit(
            "REFUSED: token name(s) already exist as Builder Variables this "
            f"tool did not mint: {sorted(result['collisions'])}. Rename them "
            "in the proposal file — adopting a foreign variable entangles "
            "the palette with records something else owns.")
    return {value: result["minted"][p["name"]]
            for value, p in accepted.items()}


def apply(site: str, *, clone_url: str = "http://127.0.0.1:8000",
          target: str = "sandbox.localhost",
          routes: list[str] | None = None, runner=None) -> dict:
    """Apply accepted proposals: records, rewrite, write-back, resolution.

    Emits the rewritten records under `opt/transforms/tokenize/` before
    touching the sandbox, so the transform is inspectable as files first.
    """
    from buildsmith.tools.sandbox import run_bench

    accepted = accepted_mapping(site)
    if not accepted:
        raise CouldNotCheck("no accepted proposals — nothing to apply")
    run = runner or run_bench

    # staleness guard: the rewrite sources block trees from the baseline
    # checkpoint. If the sandbox changed since (a re-adopt, an edit in the
    # Builder UI), applying would silently write stale trees back over it —
    # the same failure shape tokens.assert_in_sync() exists to refuse.
    _refuse_stale_checkpoint(site, target=target)

    # The gate ledger entry is written BEFORE the first mutation, so a failure
    # between minting and write-back still leaves a visible pending gate —
    # this apply lives in the library, not the CLI, precisely so no caller
    # can mutate the sandbox without the ledger knowing.
    from buildsmith.workflows.optimize import gates

    gates.record_apply(site, "tokenize", target=target)

    mapping = ensure_variables(site, accepted, target=target, runner=run)

    # persist minted uuids into the proposal file: ownership is what lets a
    # re-run update these records instead of colliding with them
    path = proposal_path(site)
    data = json.loads(path.read_text())
    for p in data["proposals"]:
        if p["value"] in mapping:
            p["uuid"] = mapping[p["value"]]
    path.write_text(json.dumps(data, indent=2) + "\n")

    pages, components = load_state(site)
    selected = _select(site, pages, components, routes)
    if not selected:
        raise SystemExit(
            f"REFUSED: route filter {routes!r} selected no pages — a filter "
            "matching nothing must not read as 'nothing needed doing'")

    out_dir = ROOT / "sites" / site / "opt" / "transforms" / "tokenize"
    out_dir.mkdir(parents=True, exist_ok=True)
    updates: dict[str, dict] = {}
    total = 0
    for label, roots in selected.items():
        before = block_ids(roots)
        replaced = rewrite_tree(roots, mapping)
        if block_ids(roots) != before:
            raise SystemExit(f"REFUSED: {label} block ids changed during a "
                             "style-only rewrite — this is a bug (TRAP-001)")
        if replaced:
            total += replaced
            kind, _, name = label.partition(":")
            updates[label] = {"kind": kind, "name": name, "blocks": roots,
                              "replacements": replaced}
            (out_dir / f"{kind}-{name}.json").write_text(
                json.dumps(updates[label], indent=2) + "\n")

    if updates:
        payload = {label: {"kind": u["kind"], "name": u["name"],
                           "blocks": u["blocks"]}
                   for label, u in updates.items()}
        script = f"""
import frappe, json
frappe.init({json.dumps(target)}); frappe.connect()
frappe.flags.in_import = True
for item in json.loads({json.dumps(json.dumps(payload))}).values():
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
print('applied', len(json.loads({json.dumps(json.dumps(payload))})))
"""
        run(script)

    missing = check_resolution(clone_url, sorted(set(mapping.values())))
    return {
        "tokens": {p["name"]: mapping[value]
                   for value, p in accepted.items()},
        "replacements": total,
        "targets": sorted(updates),
        "unresolved": missing,
        "ok": not missing,
    }


def _refuse_stale_checkpoint(site: str, *, target: str = "sandbox.localhost",
                             advise_recovery: bool = True) -> None:
    """`advise_recovery=False` for a dry-run's caught-and-warned use (#18
    review): the interrupted-apply paragraph below talks about re-applying,
    which reads as nonsense in a warning that never refuses anything, and
    its multiple paragraphs break a caller that folds the message into one
    `WARNING: {exc}` line (collapse.py's dry-run path)."""
    from buildsmith.tools import capture_dev

    manifest_path = (ROOT / "sites" / site / "opt" / "baseline" / "state"
                     / "manifest.json")
    if not manifest_path.exists():
        raise CouldNotCheck("no baseline checkpoint manifest — run "
                            "`buildsmith optimize baseline` first")
    recorded = json.loads(manifest_path.read_text())["content_hash"]
    current = capture_dev._content_hash(capture_dev.read_state(target))
    if current != recorded:
        message = (
            "REFUSED: the sandbox has changed since the baseline checkpoint "
            f"(hash {recorded[:12]} -> {current[:12]}). Re-run `buildsmith "
            "optimize baseline` so the rewrite sources current trees — "
            "applying from a stale checkpoint would overwrite the change."
        )
        if advise_recovery:
            message += (
                "\n\nIf this drift is from an apply that got interrupted "
                "(killed mid-run, crashed): do NOT re-baseline yet — that "
                "could bless a possibly-broken half-apply as the new "
                "reference forever. Run `buildsmith optimize oracle` first, "
                "against the CURRENT checkpoint. If it passes, the pending "
                "gate entry clears itself and a plain `buildsmith optimize "
                "baseline` (no --force) is safe to re-run. If it fails — or "
                "you skip it — `buildsmith optimize baseline --force` "
                "waives the still-pending entry; only do that once you have "
                "judged the half-apply's damage yourself."
            )
        raise SystemExit(message)


def check_resolution(clone_url: str, uuids: list[str],
                     *, opener=None) -> list[str]:
    """UUIDs missing from the served variables stylesheet. Empty = proved."""
    url = clone_url.rstrip("/") + "/builder_assets/variables.css"
    open_fn = opener or urllib.request.urlopen
    request = urllib.request.Request(
        url, headers={"User-Agent": "buildsmith-optimize/0.1 (tokenize)"})
    try:
        with open_fn(request, timeout=15) as response:
            css = response.read().decode("utf-8", "replace")
    except OSError as exc:
        raise CannotProve(f"cannot fetch {url}: {exc} — resolution cannot "
                          "be proved, so the apply is not proved") from exc
    # a *definition* is `--uuid:`; bare substring containment would let one
    # uuid pass on the strength of another it happens to prefix
    return [u for u in uuids
            if not re.search(rf"--{re.escape(u)}\s*:", css)]
