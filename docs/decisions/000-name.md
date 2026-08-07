# ADR-000 — The project is called `buildsmith`

**Status:** accepted · **Date:** 2026-08-03

## Context

The project needed a name before the first commit, because the name is not
cosmetic here. It becomes the Frappe app module, so it is baked into `hooks.py`,
into every exported page's `project_folder`, and into `builder_files/` paths.
Renaming later means rewriting exported fixtures, not just a directory.

## Decision

`buildsmith`.

## Consequences

- The Frappe app module is `buildsmith`; exported artifacts carry it.
- It does not change again. A rename after fixtures exist is a migration, not a
  rename.
- Neutral and unclaimed by any client, which matters for a repo intended to be
  published.
