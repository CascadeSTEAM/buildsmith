# ADR-005 — The round-trip carries pages and components, but not tokens

**Status:** ⚠ **SUPERSEDED by ADR-008 (2026-08-05)** — the token half of this
decision was wrong. Tokens *do* round-trip; the test simply never set
`frappe.flags.in_import`, without which Frappe discards a supplied record name
and every `var(--uuid)` dangles. Measured after the correction: 167/167
Builder Variable UUIDs preserved, 0 dangling references. The pages-and-
components half of this ADR still stands.

**Originally accepted** · **Date:** 2026-08-04 · **Settles:** ADR-003's deferral,
which ADR-004 withdrew but did not replace with evidence

## Context

ADR-003 deferred the standard-page round-trip on the belief it did not exist at
our Builder. ADR-004 showed that belief came from misreading a version string —
`export_import_standard_page.py` predates it — and withdrew the deferral, leaving
the real question open: *does it work, and is it worth building on?*

That is not answerable from source. So it was run.

## What was tested

`sandbox/roundtrip-check.py`, against the pinned commit:

1. Author a `Builder Page` on `sandbox.localhost` with `is_standard=1` and
   `app=builder`.
2. `on_update` calls `export_page_as_standard()`, writing
   `builder_files/pages/<scrubbed_name>/<scrubbed_name>.json`.
3. Create a second site, install the app. `after_install` / `after_migrate` call
   `sync_standard_builder_pages()`, which imports every app's `builder_files/`.
4. Compare.

## Decision

**The round-trip works.** 5/5: the page exists on the second site, and its
name, title, content and — the part that decides whether a fixture is a page or
a picture of one — its **blockIds** all survive intact. Without blockIds the
imported page could not be maintained by our tooling at all (TRAP-001).

M3's app layer is therefore viable on the current pin, with no upgrade needed.

## The caveat that decides how it is used

**Page names are `page-<hash8>` and cannot be chosen.** Verified separately:
supplying `name`, `page_name`, or both is silently ignored, because
`BuilderPage.autoname()` runs before the doctype's `field:page_name` rule and
wins, after which `page_name` is force-synced to `name` (TRAP-012).

The name is stable *once assigned* — it travels inside the fixture, which is why
the round trip preserves it. But it is not reproducible: **re-authoring the same
page produces a new hash, hence a new fixture directory, while the old one
remains.** Nothing prunes it. On the next migrate both import, and the target
site ends up with two published pages racing for one route — resolved by
`published_at desc`, silently (TRAP-010).

This was not theorised. The check hit it on its first run, having left a fixture
behind from an earlier exploratory pass, and reported two pages where it expected
one.

## Consequences

- **`builder_files/` is a valid delivery mechanism**, and M3 is a question of
  merit rather than availability. ADR-003's deferral is now closed with evidence
  rather than merely withdrawn.
- **Fixture directories must be treated as managed state, not append-only
  output.** Anything that adopts this layer needs an explicit prune step:
  reconcile `builder_files/pages/*` against the pages that should exist, and
  delete the rest. Without it, every re-author leaves a duplicate that will be
  imported onto every future site.
- **Do not design a workflow around choosing page names.** They are Builder's.
  `primitives.template.Page.record()` never sends one, and `update_payload()`
  requires one read back from the site rather than invented.
- **`sandbox/roundtrip-check.py` is the regression test**, and it asserts the
  token bug deliberately. If that check starts failing, upstream fixed it —
  which is exactly when this ADR and TRAP-013 should be revisited.
- **TRAP-009 is real and the sandbox proves it.** Authoring a component with no
  worker running hit `DocumentLockedError` on the second run, because
  `queue_action` locks the document and the queued job never executes. The check
  now authors under a system-activity flag, which is Builder's own supported
  path — not a substitute for running workers in production.

## Tokens do not survive — and this changes the answer

The first version of this ADR ended by flagging the untested case: a page that
uses a component *and* a token. That was then tested, and it found a genuine
upstream bug.

`export_variables()` receives the uuids a page references and looks each one up
by `variable_name`, or by `name` with hyphens swapped for underscores. Since
upstream's own `refactor_builder_variables` migration every variable is
uuid-named, so none of those candidates can match. The lookup falls through to
`continue`, **no `variables/` directory is written at all**, and the export
reports success. Verified by direct probe against a real record (TRAP-013).

The imported page therefore arrives with `var(--uuid)` references pointing at
variables that do not exist on the target site. It renders on its literal
fallbacks and looks almost right.

**So the honest summary is narrower than "the round trip works":**

| carries across | does not |
|---|---|
| the page, with its name and blockIds | design tokens — silently omitted |
| components, rendering their content | |

**Consequence.** `builder_files/` is usable for pages and components, but a
delivery built on it must apply tokens to the target site as a separate step and
read the applied map back **from that site** — the uuids there are its own.
Treating the fixture set as complete is the failure mode, and it is a quiet one.

## What is still untested

Fonts, client scripts, and asset files. `export_page_as_standard()` walks all of
them and each is another chance for something not to survive — as the token path
just demonstrated. Extend `roundtrip-check.py` before relying on any of them.
