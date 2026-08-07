# ADR-003 — Target Builder v1.0.0-dev; the file round-trip waits for an upgrade

**Status:** accepted, **amended the same day by [ADR-004](004-pin-by-commit.md)**
· **Date:** 2026-08-03

> **Read ADR-004 first.** Two of this decision's load-bearing claims turned out
> to be false when the version string was checked against upstream history:
> `1.0.0-dev` is a develop-branch placeholder rather than a release, and the
> round-trip it defers **already exists** in every checkout reporting that
> string. The deferral is withdrawn. What survives below is the sandbox-pinning
> principle and the capability list — the latter now dated, not versioned.

## Context

An early plan put a `builder_files/` app layer at the centre of the design: author
a page in Builder, export it as a standard page into git, `bench migrate` it onto
another site. That export/import is a **later upstream feature** and is almost
certainly absent from the Builder version actually deployed on the target site,
v1.0.0-dev.

The plan's own fallback — "build the app layer anyway" — would have passed a
sandbox test and delivered nothing, because the sandbox would have been running a
newer Builder than the site.

## Decision

- **Target v1.0.0-dev.** Deliver value on the version actually running.
- **The sandbox pins the target's Builder commit**, so sandbox results are
  honest. A second pin tracks upstream for evaluating an upgrade.
- **The `builder_files/` round-trip is deferred**, gated on a Builder upgrade
  raised as its own operations ticket.

## What v1.0.0-dev does have

Verified the hard way, and enough to build the whole design system on:

- `is_template` + `template_group`
- `Builder Variable` (`Color` | `Dimension`, with `dark_value`)
- `Builder Component` (`autoname: field:component_id`)
- repeaters with `dataKey`
- `Builder Snapshot`
- `extend_block()` shell semantics

## Consequences

- Tokens + components + template + data-driven pages carry the near-term
  milestones; none of them depend on the round-trip.
- If the upgrade never happens, the project still delivers. The round-trip is
  genuinely optional, not a load-bearing assumption.
- Before assuming any Builder capability, check it against the pin. The trap
  ledger exists because several of these were discovered by breaking a live site.
