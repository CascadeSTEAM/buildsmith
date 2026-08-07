---
name: builder-safety
description: Load BEFORE any Frappe Builder component or page write — traps, simulate, snapshot-first, and the developer_mode procedure
mode: skill
triggers: builder,component,page,block,token,variable,publish,write,simulate,snapshot
---

# builder-safety

**Load this before writing anything to Builder.** Not after planning, not when
something looks risky — before. Several entries in the trap ledger cost real
damage on a live site, and every one of them failed *silently*: the write
succeeded, the page rendered, and the wrong thing was on screen.

## First, the access path

Load the operations project's **`frappe-access`** skill and take the path it
dictates: **Path A** (API) by default, **Path B** (admin exec) only where the
operation genuinely needs it. Never hand-roll `ssh … docker exec … bench
console`.

Buildsmith itself writes nothing. Its tools emit files; applying them is an
action, and actions belong to the operations project (ADR-002).

## The order, every time

1. **Read `docs/traps.md`.** All of it. It is short and it is the accumulated
   cost of not having read it.
2. **Snapshot / back up.** A `Builder Snapshot` is the cheap safety net that
   makes "watch it land live" an acceptable posture.
3. **Read the current state back** — the component tree, the token map. Never
   reconstruct it from what you think you wrote last time.
4. **Simulate.** `buildsmith simulate --state <export> --payload <payload>`. A
   non-zero exit means this write would collapse pages that currently work.
5. **Validate.** `buildsmith validate` on every payload before handoff.
6. **Apply** via an operations subagent, then **read the state back again**.
7. **Journal it.** `buildsmith journal append`.

## The four that will catch you

- **TRAP-001.** Pages hold empty override shells, not the component. Re-issuing
  a component's blockIds renders every consuming page as `element=None`. And
  the mirror: a child *added* to a component renders nowhere on existing pages,
  because no shell references it. Neither self-heals — `on_update` does not call
  `sync_component()`.
- **TRAP-003.** A repeater needs `isRepeaterBlock` **and** `children` **and**
  `dataKey`. Missing any one degrades it to an ordinary block, silently. Never
  hand-assemble one; use `primitives.repeater.repeater()`.
- **TRAP-007.** Never delete a `Builder Variable`. References do not cascade and
  nothing warns. Rename or remap in place.
- **TRAP-006.** A template page with a `template_group` needs developer_mode on
  the **live site**, and saving it writes fixture files onto the server.
  `primitives.template.side_effects()` lists exactly what happens.

## What "verified" means here

Against the **pinned commit** in `sandbox/pins.env`, not a version number.
Builder's develop branch reports `1.0.0-dev` across more than a thousand
commits, and a doctype half the ledger depends on was renamed inside that range
(ADR-004). Before trusting any of this on a different Builder, re-run
`buildsmith check traps` and `buildsmith check simulate` against that pin.
