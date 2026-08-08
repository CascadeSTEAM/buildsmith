#!/usr/bin/env python3
"""The run journal: what was emitted, from what, against which Builder.

A site is maintainable a year later only if you can answer "why is it like
this?" without archaeology. Every tool run appends one JSON record here, and
`render` turns the accumulated records into a build log fit for a ticket.

The journal lives in the **private layer** (`sites/<site>/journal/`), which is
gitignored. That is deliberate: a record names real routes, counts and file
paths, and those are facts about a client's site even when no token appears in
them. Keeping it beside the artifacts it describes rather than in a shared log
is the same ownership rule the rest of the project follows.

Records are append-only, one file per day, JSON Lines. Append-only because a
journal you can quietly edit answers "why is it like this?" with whatever the
last person wanted it to say.

Usage:
    buildsmith journal append --site example --tool theme --input tokens.json \\
        --output build/ --count components=11 --warning "3 orphan tokens"
    buildsmith journal render --site example
    buildsmith journal render --site example --since 2026-08-01

Nothing here touches a site.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from buildsmith.tools.gitenv import run_git

ROOT = Path(__file__).resolve().parents[2]

__all__ = ["Entry", "append", "journal_dir", "read_entries", "render"]


def journal_dir(site: str, *, root: Path | None = None) -> Path:
    return (root or ROOT) / "sites" / site / "journal"


def _builder_pin(root: Path | None = None) -> dict[str, str]:
    """The pin the artifacts were built against.

    Recorded on every entry because it is the single fact that makes an old
    record interpretable: a payload built against one Builder commit may be
    wrong for another, and `1.0.0-dev` identifies nothing (ADR-004).
    """
    pins = (root or ROOT) / "sandbox" / "pins.env"
    values: dict[str, str] = {}
    if pins.exists():
        for line in pins.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() in {"BUILDER_REF", "BUILDER_REF_STATUS", "FRAPPE_REF"}:
                values[key.strip()] = value.split("#")[0].strip()
    return values


def _tooling_revision(root: Path | None = None) -> str:
    """Which Buildsmith produced this. Best effort — a dirty tree says so."""
    try:
        rev = run_git("-C", str(root or ROOT), "rev-parse", "--short", "HEAD", timeout=5)
        if rev.returncode != 0:
            return "unknown"
        head = rev.stdout.strip()
        dirty = run_git("-C", str(root or ROOT), "status", "--porcelain", timeout=5)
        return f"{head}-dirty" if dirty.stdout.strip() else head
    except (OSError, subprocess.SubprocessError):
        return "unknown"


@dataclass
class Entry:
    """One tool run."""

    tool: str
    timestamp: str
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    builder: dict[str, str] = field(default_factory=dict)
    tooling: str = "unknown"
    notes: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def append(
    site: str,
    tool: str,
    *,
    inputs: list[str] | None = None,
    outputs: list[str] | None = None,
    counts: dict[str, int] | None = None,
    warnings: list[str] | None = None,
    notes: str = "",
    root: Path | None = None,
    now: datetime | None = None,
) -> Entry:
    """Append one record. Returns it, so a caller can log what it wrote."""
    moment = now or datetime.now(UTC)
    entry = Entry(
        tool=tool,
        timestamp=moment.isoformat(timespec="seconds"),
        inputs=sorted(inputs or []),
        outputs=sorted(outputs or []),
        counts=dict(counts or {}),
        warnings=list(warnings or []),
        builder=_builder_pin(root),
        tooling=_tooling_revision(root),
        notes=notes,
    )

    directory = journal_dir(site, root=root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{moment.strftime('%Y-%m-%d')}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(entry.to_json() + "\n")
    return entry


def read_entries(site: str, *, root: Path | None = None, since: str | None = None) -> list[Entry]:
    directory = journal_dir(site, root=root)
    if not directory.exists():
        return []

    entries: list[Entry] = []
    for path in sorted(directory.glob("*.jsonl")):
        if since and path.stem < since:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(Entry(**json.loads(line)))
            except (json.JSONDecodeError, TypeError) as exc:
                # A corrupt line must not silently vanish from the build log —
                # a journal with a hole in it is worse than one that says so.
                entries.append(
                    Entry(
                        tool="UNREADABLE",
                        timestamp=f"{path.stem}?",
                        notes=f"{path.name}:{number} could not be parsed: {exc}",
                    )
                )
    return sorted(entries, key=lambda e: e.timestamp)


def render(site: str, *, root: Path | None = None, since: str | None = None) -> str:
    """Turn the journal into a build log."""
    entries = read_entries(site, root=root, since=since)
    if not entries:
        return (
            f"# Build log — {site}\n\n"
            "_No journal entries._ Either nothing has been run for this site, or the "
            "runs did not journal — which is itself worth chasing, since an unrecorded "
            "build is one nobody can explain later.\n"
        )

    lines = [f"# Build log — {site}", ""]

    pins = {e.builder.get("BUILDER_REF", "") for e in entries if e.builder}
    pins.discard("")
    if len(pins) > 1:
        lines += [
            "> **These runs span more than one Builder commit.** Payloads built against "
            "different commits may not mean the same thing; check before treating this "
            "log as one continuous history.",
            "",
        ]

    lines += [f"{len(entries)} run(s) recorded.", ""]

    for entry in entries:
        lines.append(f"## {entry.timestamp} — {entry.tool}")
        lines.append("")
        if entry.notes:
            lines += [entry.notes, ""]
        if entry.counts:
            lines.append("| what | count |")
            lines.append("|---|---|")
            lines += [f"| {k} | {v} |" for k, v in sorted(entry.counts.items())]
            lines.append("")
        if entry.inputs:
            lines += ["**Inputs**", ""] + [f"- `{i}`" for i in entry.inputs] + [""]
        if entry.outputs:
            lines += ["**Outputs**", ""] + [f"- `{o}`" for o in entry.outputs] + [""]
        if entry.warnings:
            lines += ["**Warnings**", ""] + [f"- {w}" for w in entry.warnings] + [""]

        pin = entry.builder.get("BUILDER_REF", "unknown")
        status = entry.builder.get("BUILDER_REF_STATUS", "")
        suffix = f" ({status})" if status else ""
        lines += [f"_Builder `{pin}`{suffix} · buildsmith `{entry.tooling}`_", ""]

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("append", help="append one run record")
    add.add_argument("--site", required=True)
    add.add_argument("--tool", required=True)
    add.add_argument("--input", action="append", default=[], dest="inputs")
    add.add_argument("--output", action="append", default=[], dest="outputs")
    add.add_argument("--count", action="append", default=[], metavar="NAME=N")
    add.add_argument("--warning", action="append", default=[], dest="warnings")
    add.add_argument("--note", default="")

    show = sub.add_parser("render", help="render the build log")
    show.add_argument("--site", required=True)
    show.add_argument("--since", help="YYYY-MM-DD; skip earlier days")
    show.add_argument("--out", help="write here instead of stdout")

    args = parser.parse_args(argv)

    if args.command == "append":
        counts: dict[str, int] = {}
        for pair in args.count:
            name, _, value = pair.partition("=")
            if not value.strip().lstrip("-").isdigit():
                parser.error(f"--count expects NAME=<integer>, got {pair!r}")
            counts[name.strip()] = int(value)
        entry = append(
            args.site,
            args.tool,
            inputs=args.inputs,
            outputs=args.outputs,
            counts=counts,
            warnings=args.warnings,
            notes=args.note,
        )
        print(f"journalled {entry.tool} at {entry.timestamp} for site '{args.site}'")
        return 0

    text = render(args.site, since=args.since)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
