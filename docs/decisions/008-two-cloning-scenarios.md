# ADR-008 — Two cloning scenarios, two contracts

**Status:** accepted · 2026-08-05 · owner's framing
**Supersedes** ADR-005's conclusion. **Corrects** TRAP-012 and TRAP-013.

## Context

Buildsmith had one clone path: crawl a site's rendered HTML and reconstruct
Builder blocks from it. It was used for everything, and it produced a long tail
of "completeness" failures that we fixed one symptom at a time — assets missed,
CSS that would not fold onto blocks, scripts carried but never rendered, an
`<html>` root, font stacks the editor cannot parse.

They were not separate bugs. Our own crawl proved the source site serves
`/assets/builder/reset.css`, `/builder_assets/variables.css` and 1714 `fb-*`
classes: **it was already a Frappe Builder site.** We were scraping the rendered
output of a Builder site and reverse-engineering it back into Builder blocks,
while the authoritative `Builder Page.blocks` sat on the server the whole time.

The owner's framing, which is the right one:

> 1. an existing non-Frappe site that we are about to import, upgrade and make
>    "POP" on our new server for them
> 2. an existing customer on our own server that we need to work on

## Decision

Two paths, with **opposite contracts**.

### Import — `workflows/replicate`

Source is not Frappe. Crawl, convert, and produce a **new** Builder site that
resembles the old one. Fidelity is the acceptance gate, not the deliverable:
the point is a site that is editable, maintainable and better than what it
replaces. Reconstruction loss is expected, and must be reported rather than
hidden. A `clone-diff` finding here is information, not necessarily a defect.

### Maintain — `tools/adopt`

Source is already Frappe Builder. The operations project exports the Builder
doctypes (an action, ADR-002); this consumes the files. **Copy the records.**
Exactness is the contract and any difference is a defect.

Not a full `bench backup`/`restore`, deliberately: a restore drags users,
sessions, API keys and any ERP data onto a dev box, and for a tool whose ethos
is not holding client data that cost is real. The Builder doctypes are what we
need.

## What this corrected

Adopting requires `frappe.flags.in_import`. Frappe's `set_new_name()` does

```python
if autoname.lower() not in ("prompt", "uuid") and not frappe.flags.in_import:
    doc.name = None
```

so a supplied `name` is discarded on every ordinary insert — including every
REST insert, since there is no whitelisted way to set the flag. Setting it
changes three previously-recorded conclusions:

- **ADR-005 / TRAP-013 said design tokens do not survive a round-trip.** They
  do. `Builder Variable` names are UUIDs that block styles reference as
  `var(--<uuid>)`; without the flag Frappe mints new ones and every reference
  dangles, so the page renders on its literal fallbacks and looks *almost*
  right. Measured after the fix: **167/167 UUIDs preserved, 0 dangling.**
- **TRAP-012 said a page's name is an unchooseable hash.** It is choosable.
- **BS-010** (fixture directories accumulating because re-authoring mints a new
  page) loses its root cause: a re-adopt now updates in place. Measured: a
  second run reported 167 *updated*, 0 inserted.

The same flag also suppresses `BuilderComponent.on_update`'s `queue_action`,
which locks the document and enqueues a job (TRAP-009, TRAP-017).

## Consequences

- Adopting runs through `bench`, not REST, because of the flag. That is a
  constraint on the container story: day-to-day editing and capture use REST and
  need no flag, but adopting a site needs bench. Acceptable — adopting is rare
  and operator-shaped; editing is frequent and designer-shaped.
- `adopt` must detect route collisions. Two published pages on one route do not
  error: Builder resolves with `published_at desc, creation desc`, so one
  silently wins and the other becomes unreachable (TRAP-010). Found the first
  time it ran, against real data.
- The scraper is still needed, and still worth improving, for scenario 1. The
  Frappe source doubles as a **calibration harness** for it: scrape the site,
  diff the reconstruction against the real records, and measure precisely what
  the converter loses instead of discovering it one symptom at a time.
