# ADR-001 — MIT licence

**Status:** accepted · **Date:** 2026-08-03

## Context

Frappe Builder is AGPL-3.0. The obvious worry is that a project built around it
inherits that obligation.

## Decision

MIT.

## Consequences

AGPL-3.0 binds derivative works of Builder's *source*. We vendor none of it:

- The block schema we emit is **data**, not Builder code.
- `builder_templates/` fixtures are our own authored content.
- No tool here imports Builder, links against it, or embeds any part of it. The
  tools read files and write files; something else applies the result.

If that ever stops being true — if we vendor Builder source or link against it —
this decision has to be revisited before the code ships. Note the boundary is
about *our* distribution: an operator running Builder itself is still bound by
Builder's own licence, and nothing here changes that.
