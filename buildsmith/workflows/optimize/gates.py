"""The gate ledger — the machine-checkable record that a transform was proved.

ADR-009's contract is that the rendering oracle gates every Phase A
transform. Before this module existed the gate was real for `collapse` and a
printed suggestion for `tokenize` and `fonts` — and re-baselining absorbed
any unproven change into the new reference forever, because nothing recorded
whether the oracle ever ran, let alone passed.

The ledger (`sites/<site>/opt/gates.json`, private layer) holds one entry
per applied transform: which baseline content-hash it was applied against,
and the oracle verdict once one runs. An entry with no passing verdict is
**pending**, and `buildsmith optimize baseline` refuses to overwrite the
reference while any entry is pending — `--force` waives them, and the
waiver itself is written down, so the ledger never lies by omission.

Nothing here deletes an entry. The ledger is append-and-annotate, like the
proposal files: it is the decision record for "was this change proved?".
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

__all__ = [
    "any_pending",
    "assert_no_pending",
    "pending",
    "record_apply",
    "record_oracle",
    "transform_names",
]


def _path(site: str) -> Path:
    return ROOT / "sites" / site / "opt" / "gates.json"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _load(site: str) -> dict:
    path = _path(site)
    if not path.exists():
        return {"entries": []}
    return json.loads(path.read_text())


def _save(site: str, data: dict) -> None:
    path = _path(site)
    path.parent.mkdir(parents=True, exist_ok=True)
    # tmp + rename: a crash mid-write must not leave a truncated ledger —
    # an unreadable ledger blocks every transform behind a JSON traceback.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def baseline_hash(site: str) -> str | None:
    """The current baseline checkpoint's content hash, if one exists."""
    manifest = ROOT / "sites" / site / "opt" / "baseline" / "state" / "manifest.json"
    if not manifest.exists():
        return None
    return json.loads(manifest.read_text()).get("content_hash")


def record_apply(site: str, transform: str, *, target: str = "sandbox.localhost") -> dict:
    """Record that `transform` mutated `target`. Pending until proved.

    Called immediately after an apply returns — even one whose own assertion
    failed — because the mutation has happened either way, and a mutation
    with no oracle verdict is exactly what the ledger exists to surface.

    `target` matters because every optimize apply accepts `--target` (#20's
    review): a pending entry against some other host says nothing about
    whether `sandbox.localhost` itself is dirty.
    """
    data = _load(site)
    entry = {
        "transform": transform,
        "target": target,
        "baseline_hash": baseline_hash(site),
        "applied_at": _now(),
        "oracle": None,
        "waived": False,
    }
    data["entries"].append(entry)
    _save(site, data)
    return entry


def transform_names(entries: list[dict]) -> str:
    """A sorted, deduped, comma-joined list of entries' transform names.

    One place for the message every pending-entry refusal builds, so the
    `.get` fail-closed convention this module documents (a human may edit
    the ledger; a missing key must read as "not proved", never crash the
    check that's about to report it) applies wherever entries are reported,
    not just wherever `_is_pending` reads them.
    """
    return ", ".join(sorted({e.get("transform", "unknown") for e in entries}))


def _is_pending(entry: dict) -> bool:
    """Applied, not waived, and no passing oracle verdict yet.

    `.get` throughout: the ledger is a file a human may edit, and a missing
    key must read as "not proved" (fails closed), not crash the transform
    that just mutated the sandbox.
    """
    return (not entry.get("waived")
            and not (entry.get("oracle") or {}).get("ok"))


def record_oracle(site: str, ok: bool, failed: int = 0) -> int:
    """Attach an oracle verdict to every pending entry. Returns how many.

    A passing verdict clears them; a failing one is written too — the entry
    stays pending (the change is applied and provably NOT equivalent), and
    the record shows the proof was attempted.
    """
    data = _load(site)
    marked = 0
    for entry in data["entries"]:
        if _is_pending(entry):
            entry["oracle"] = {"ok": ok, "failed": failed, "at": _now()}
            marked += 1
    if marked:
        _save(site, data)
    return marked


def pending(site: str) -> list[dict]:
    """Entries applied to the sandbox but never proved by a passing oracle."""
    return [entry for entry in _load(site)["entries"] if _is_pending(entry)]


def any_pending(target: str = "sandbox.localhost") -> dict[str, list[dict]]:
    """Every site with an applied-but-unproved transform against `target`.

    Every optimize apply accepts `--target`, so a pending entry does not by
    itself mean `target` is dirty — only an entry recorded *against* it
    does. An entry with no `target` key (written before this field existed)
    still counts: fails closed, the same way a missing `oracle` counts as
    unproved rather than assumed-fine (`_is_pending`).

    A tool about to mutate `target` in a way that isn't scoped to its own
    fixtures (#20) should refuse while this is non-empty, the same way
    `assert_no_pending` refuses a re-baseline.
    """
    sites_dir = ROOT / "sites"
    if not sites_dir.is_dir():
        return {}
    found = {}
    for site_dir in sorted(sites_dir.iterdir()):
        if not site_dir.is_dir():
            continue
        open_entries = [
            e for e in pending(site_dir.name)
            if e.get("target", target) == target
        ]
        if open_entries:
            found[site_dir.name] = open_entries
    return found


def assert_no_pending(site: str, *, force: bool = False) -> list[dict]:
    """Refuse while any applied transform lacks a passing oracle.

    This is the re-baseline gate: without it, `apply -> skip the oracle ->
    baseline` absorbs an unproven visual change into the reference forever.
    With `force=True` the pending entries are waived — and the waiver is
    recorded, so a later reader can see the proof was skipped on purpose.
    Returns whatever was waived.
    """
    open_entries = pending(site)
    if not open_entries:
        return []
    if not force:
        names = transform_names(open_entries)
        raise SystemExit(
            f"{len(open_entries)} applied transform(s) have no passing oracle: "
            f"{names}. Re-baselining now would absorb the unproven change into "
            "the reference forever. Run `buildsmith optimize oracle` first — "
            "or --force to waive, which is recorded in the ledger."
        )
    data = _load(site)
    waived = []
    for entry in data["entries"]:
        if _is_pending(entry):
            entry["waived"] = True
            entry["waived_at"] = _now()
            waived.append(entry)
    _save(site, data)
    return waived
