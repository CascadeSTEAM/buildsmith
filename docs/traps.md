# Trap ledger

Frappe Builder failure modes learned the hard way. **Load this before composing
or writing any component or page.** Several of these cost real damage on a live
site; none of them announce themselves, and most fail *silently* — the write
succeeds, the page renders, and the wrong thing is on screen.

Each entry has a **symptom** (what you observe), a **rule** (what to do), and a
**test** status:

| marker | meaning |
|---|---|
| ★ | expressible as a test — our tooling must be unable to commit it |
| — | procedural; a rule for the operator, not something code can enforce |

Every ★ trap gets a case in `tests/test_traps.py`. A trap without a test is a
trap we will hit again.

Applies to the **pinned Builder commit** (`sandbox/pins.env`), not to a version
number — Builder's develop branch reports the same version string across more
than a thousand commits, and a doctype half this ledger depends on was renamed
inside that range. See ADR-004. Moving the pin means re-verifying every entry
here, not assuming they carry over.

---

## ★ TRAP-001 — Replacing a component's `block` collapses it across every page

**Symptom.** You compose a fresh block tree for an existing, in-use component and
write it to `Builder Component.block`. Every page consuming that component loses
its interior: nodes collapse to `element=None`. The most expensive near-miss on
record would have wiped the header and footer across all 13 pages of a live site.
It was caught only because a subagent simulated the write first.

**Cause.** Pages don't embed the component — they hold a mirror of empty
*override shells*, and `extend_block()` rebuilds the visible tree at render time.
The loop, verified at the pin, iterates the **page's** shells:

```python
for overridden_child in overridden_children:          # the page's shells
    component_child = first child whose blockId is in
        (overridden_child.blockId, overridden_child.referenceBlockId)
    if component_child: merge the two
    else:               keep the bare shell           # <- the collapse
```

A shell that matches nothing is emitted exactly as `reset_block_styles()` left
it: `element=None`, `innerHTML=None`, empty styles. A freshly-composed tree has
new ids, so nothing matches. And `on_update` does **not** call
`sync_component()` — it clears caches and mints a version — so the damage lands
silently and shows up later, on a page nobody was looking at.

**The mirror case, and the easier one to miss.** Because the loop iterates the
page's shells, a child *added* to a component is referenced by no shell on any
existing page and is therefore **never rendered there**. It looks correct in the
Builder editor and is absent in production, with no error anywhere. Verified at
the pin alongside the collapse.

**Rule.** Preserve blockIds and restyle **in place**. If the structure genuinely
must change — including any addition — run `ComponentSyncer` across every
consuming page in the same operation. `primitives/components.py` enforces both
halves: `revise()` refuses a tree that drops ids, and refuses one that adds them
unless `allow_additions=True` records that a sync is required. Never hand a
component payload to an operator without a clean `buildsmith simulate` run.

## — TRAP-002 — A composed component carries no content

**Symptom.** You swap in a newly-composed header and the nav links, address and
phone number are gone. Nothing errored.

**Cause.** A composed component is a structural *skeleton*. Content lives in the
block tree you replaced.

**Rule.** Composition and content are separate steps. Read the existing content
out first, or compose from a source that includes it. Related to TRAP-001 — the
same swap that drops content is the one that collapses shells.

## ★ TRAP-003 — Repeaters have six independent silent-failure modes

**Symptom.** The repeater renders as an ordinary block: one item instead of N, or
raw Jinja on the page. No error, no warning.

**Cause.** Six separate requirements, any one of which fails quietly:

1. The container needs `isRepeaterBlock` **and** `children` **and** `dataKey`.
   Missing any one degrades it to a normal block. At the pin this is one
   expression — `is_repeater_block()` ands all three — so there is no partial
   credit and no warning.
2. Only `children[0]` repeats. **Later siblings are not rendered at all** —
   `render_repeater_children()` reads `children[0]` and appends only that. An
   earlier note here said siblings "render once"; they do not render. The
   sandbox check asserts the stricter behaviour.
3. The loop iterates `dataKey.key`. Setting `property` on the container binds the
   *container*, not the iteration — a common and confusing mix-up.
4. `src` and `href` bindings need `type: "attribute"`. Without it **no attribute
   is emitted at all** — verified at the pin, an `img` with an unmarked `src`
   binding renders as `<img class="">`, the binding simply gone.
5. ~~The same binding present in **both** `dataKey` and `dynamicValues` leaks raw
   Jinja into the output.~~ **Fixed upstream before the current pin** —
   `set_dynamic_content_placeholders` dedupes by `(property, type)`. Verified:
   the output is one clean placeholder. `primitives/repeater.py` still refuses
   duplicates, because an emitter should not depend on a downstream fix for its
   correctness and a pin that moves backwards would bring the leak back.
6. `visibilityCondition` is **not** evaluated on a repeater's immediate child.
   Verified at the pin: `render_children()` sets a visibility key per child,
   `render_repeater_children()` does not. The condition is accepted, stored, and
   never consulted — so the block is always visible.

**Rule.** Never hand-assemble a repeater. `primitives/repeater.py` enforces all
six structurally; anything else is a future bug. Rule 1 is what the sandbox
reproduces as its faithfulness check (see `sandbox/README.md`).

## ★ TRAP-011 — `Builder Variable` was renamed to `Builder Token` upstream

**Symptom.** Every token write fails with an unknown-doctype error, on a Builder
that still reports version `1.0.0-dev`.

**Cause.** Upstream commit `f0781da9`, 2026-07-16, `refactor!: rename Builder
Variable to Builder Token`. It is *inside* the `1.0.0-dev` version string, so the
reported version does not change across it (ADR-004).

**Rule.** Never hardcode the doctype name. `primitives/tokens.py` keeps it behind
a single constant selected from the pin, so the migration is one line rather than
a sweep. Before moving the pin, check which side of `f0781da9` it lands on —
this is the specific reason the pin is a commit and not a version string.

**Consequence for the entries below:** TRAP-004 and TRAP-007 are written against
`Builder Variable`, which is correct at the current pin. Past `f0781da9` the
rules still hold; the doctype name does not.

## — TRAP-015 — A replaced page 403s until the route cache is cleared

**Symptom.** You apply pages, browse the site, and every route returns
**403 Not Permitted** — including routes that worked minutes earlier. The
records look perfect: `published=1`, correct route, `authenticated_access=0`.

**Cause.** `find_page_with_path()` is `@redis_cache(ttl=3600)`. Deleting a page
and creating its replacement mints a **new** docname (TRAP-012), but the cache
still maps the route to the old, now-deleted name. The renderer resolves the
route, fails to load the document, and the failure surfaces as a permission
error rather than a 404 — which sends you hunting through permissions instead of
the cache.

**Rule.** Clear the website cache immediately after applying pages, before
looking at the site: `bench --site <site> clear-website-cache`. Then browse as an
**anonymous** visitor — logged in as admin you may not see it at all. Both the
go-live plan and the handoff brief now say so.

## ★ TRAP-014 — An empty route is not "the home page"

**Symptom.** You replicate a site, publish it, and `/` still serves the Frappe
desk login. The homepage exists and is published, but is nowhere you can find.

**Cause.** `BuilderPage.set_default_values()` rewrites an empty route:

```python
if not self.route:
    if not self.name:
        self.autoname()
    self.route = f"pages/{self.name}"
```

So a page created with `route=""` lands at `pages/page-0522e317` — an
unpredictable hash URL, since the name is not chooseable either (TRAP-012). And
`/` is not a route at all: the site's front door is
`Website Settings.home_page`, which nothing sets for you.

**Rule.** Give the home page a real route — `primitives.template.HOME_ROUTE`,
i.e. `home` — and set `Website Settings.home_page` to it.
`primitives.template.page()` refuses an empty route outright, and
`prerequisites()` lists the setting so it reaches the go-live plan and the
handoff brief.

## — TRAP-016 — Commit dates lie; only topology tells you what is in a branch

**Symptom.** You compare two commit dates, conclude one came first, and reason
about whether a change is present. The conclusion is confidently wrong.

**Cause.** A rebase rewrites committer dates. Upstream's whole
`Builder Variable → Builder Token` series carries **one identical committer
timestamp**, `f0781da9` among them at 2026-07-16 — *earlier* than the commit our
target actually runs (`b09a40d9`, 2026-07-20). And yet:

```
git merge-base --is-ancestor f0781da9 b09a40d9   →  NO
```

The rename was not in that build. It only reached `develop` later, through the
merge `85dc4946` on 2026-07-26. A `commits?sha=develop` listing sorted by date
puts it in the wrong place entirely.

**Rule.** Never order commits by date to decide what a build contains. Use
`git merge-base --is-ancestor`, or check for the artifact directly — does the
doctype directory exist on the host? Two of this project's own version
conclusions were reached by date comparison, and ADR-004 was written *in that
style* while arguing against exactly this class of error.

**Live consequence.** `builder_token` **is** an ancestor of current `develop`.
The target does not have it today, and will the moment its image is rebuilt off
`develop`. TRAP-011 is not hypothetical; it is scheduled.

## ★ TRAP-013 — Design tokens do not survive the `builder_files/` round trip

> **CORRECTED 2026-08-05 (ADR-008).** They survive. `Builder Variable` names
> are UUIDs that block styles reference as `var(--<uuid>)`; without
> `frappe.flags.in_import` Frappe mints new ones, every reference dangles, and
> the page renders on its literal fallbacks looking *almost* right — which is
> what this entry recorded. Measured with the flag set: **167/167 UUIDs
> preserved, 0 dangling references.**

**Symptom.** A standard page imported onto a second site renders in its literal
fallback colours. Nothing errors; it looks *almost* right, and the design system
is entirely disconnected.

**Cause — an upstream bug, verified at the pin.** `export_page_as_standard()`
extracts the variables a page references (correctly — it finds the uuids) and
hands them to `export_variables()`, which then looks each one up by:

1. `variable_name` in `[uuid, uuid_with_underscores, "Uuid Title Case"]`, and
2. failing that, `name` == the uuid **with hyphens replaced by underscores**.

Every variable is uuid-*named* since upstream's own `refactor_builder_variables`
migration, and none of those four candidates can ever match a uuid. So the lookup
falls through to `continue`, no `variables/` directory is written, and the export
completes successfully having silently omitted every token. The importing site
then has `var(--uuid)` references pointing at variables that do not exist.

Confirmed by direct probe: for a real record named
`d3adb101-5c2e-4a1f-9e77-000000000000` (rewritten here), all four candidate lookups return
nothing.

**Rule.** Pages and components round-trip; **tokens do not**. If you use
`builder_files/`, the tokens have to be applied to the target site separately —
`primitives.tokens` emits exactly that plan, and `Applied` must then be read back
from the *target* so references resolve there.

`sandbox/roundtrip-check.py` asserts this bug on purpose. If that check starts
failing, upstream fixed it — delete the inversion and update this entry and
ADR-005.

## ★ TRAP-012 — A Builder Page's name is not yours to choose

> **CORRECTED 2026-08-05 (ADR-008).** It is choosable. Frappe's
> `set_new_name()` discards a supplied `name` only when
> `frappe.flags.in_import` is unset; with the flag, `page_name` becomes the
> docname. Everything below still holds for any ordinary insert — including
> every REST insert, since no whitelisted method can set the flag — so treat
> this as true unless you are importing through bench.

**Symptom.** You create a page with a deliberate name, and it comes back as
`page-3f9c1a02`. Or an update creates a second page instead of changing the one
you meant.

**Cause.** `BuilderPage.autoname()` assigns `page-<hash8>` and runs *before* the
doctype's `field:page_name` rule, so it wins; `page_name` is then force-synced to
`name`. Verified at the pin — supplying `name`, `page_name` or **both** is
silently ignored in all three cases.

Contrast `Builder Component`, where `component_id` *is* yours (TRAP-005). The two
doctypes behave oppositely, and assuming either rule applies to the other is the
mistake.

**Rule.** Never send `name` when creating a page —
`primitives.template.Page.record()` does not. To update one, read its name back
from the site; `update_payload()` refuses without it rather than silently
creating a duplicate.

**Consequence for fixtures.** Exported standard pages live in
`builder_files/pages/<scrubbed_name>/`, so the directory carries that hash.
Re-authoring the same page mints a new hash and a new directory while the old one
stays, and both import on the next migrate — two published pages, one route, the
newest silently winning (ADR-005, TRAP-010). Fixture directories are managed
state and need pruning; nothing prunes them for you.

## ★ TRAP-004 — `Builder Variable.type` is only `Color` or `Dimension`

**Symptom.** You tokenise a font family, a font weight, a unitless line-height, a
box-shadow or an easing curve, and it does not apply.

**Cause.** Those are the only two types. There is no escape hatch.

**Rule.** Anything outside Color/Dimension becomes a **component prop** plus one
injected `head_html` stylesheet. Do not fake it with a Dimension.

`dark_value` lives on the same record. **Builder composes `light-dark()` itself**
from `value` and `dark_value`, and only when the two differ — so supply plain
values and never pre-compose it, or you nest one inside another.
`primitives/tokens.py` refuses a pre-composed value and drops a `dark_value`
identical to the light one.

Reference tokens as `var(--uuid, literal)` with the literal as the fallback, so a
missing variable degrades to the right colour instead of to nothing. Note the
related silent failure: `get_css_variables()` skips any variable whose `value` is
empty, so an empty value emits no CSS variable at all and *every* reference to it
quietly falls back — the page looks almost right.

## ★ TRAP-005 — `component_id` and `name` must not diverge

**Symptom.** `clear_page_cache()` appears to work but pages keep serving stale
component markup.

**Cause.** `Builder Component` is `autoname: field:component_id`, and Frappe
force-syncs the field to `name`. Pages reference `name`; `clear_page_cache()`
matches on `component_id`. Let the two drift and cache invalidation silently
targets nothing.

**Rule.** Treat them as one value. Never set `component_id` on an existing record.

## — TRAP-006 — a *shipped template group* needs developer_mode, and writes files

**Symptom.** Saving a template page throws `PermissionError` — "Template pages
can only be modified in developer mode". Or, more surprisingly: files appear
inside the Builder app directory on a live server, with no deploy having run.

**Cause, corrected.** An earlier version of this entry said `is_template=1`
requires developer_mode. That is **wrong**, and the distinction matters:

```python
if self.is_template and self.template_group and not developer_mode
        and not is_system_activity():
    frappe.throw("Template pages can only be modified in developer mode.")
```

The gate needs **both** fields. `is_template` alone — a user's "save as
template" — is ungated, and the fixture sync deliberately leaves it alone.
`is_system_activity()` (install / migrate / patch / import / test) is exempt.

**The side effect nobody had written down.** When both fields *are* set and
developer_mode is on, `on_update` calls `export_template_group()`, which writes
the entire group — every page, component, variable, client script and font — to
`<app>/builder/builder_templates/<group>/`, plus assets to
`<app>/builder/www/builder_assets/<group>/`. Deleting such a page removes its
fixtures in developer mode, and throws in production.

This is not hypothetical: a version audit of one deployment found 14
runtime-written files under exactly those two paths, absent from the container
image and unexplained until this code was read.

**Rule.** Know which kind you are making. For a shipped group: enable
developer_mode → apply → disable, and expect files on the host.
`primitives.template.side_effects()` returns the full list for a payload, so a
go-live plan can print it rather than someone discovering it afterwards. And the
dev-mode requirement is a procedure, not an impossibility — it does not mean you
cannot design against production.

## ★ TRAP-007 — Never delete a `Builder Variable`

**Symptom.** Colours across the site fall back to browser defaults, on pages you
never touched.

**Cause.** Deletion does not cascade. One page alone was found holding 50
`var(--uuid)` references.

**Rule.** **Rename or remap in place.** Deleting and recreating produces a new
uuid, and every existing reference silently points at nothing. There is no
rollback short of restoring a snapshot.

## — TRAP-008 — A blank `time_zone` stores timestamps in IST

**Symptom.** A record's `creation` reads `2026-08-01 07:09` when you created it
on the evening of 2026-07-31.

**Cause.** `System Settings.time_zone` left blank. That example is `2026-07-31
18:39 PDT` — an offset large enough to move the date.

**Rule.** Convert before concluding anything from a timestamp. This has already
caused a stale probe result to be trusted as fresh. Settle the site's timezone
*before* creating dated records, not after.

## — TRAP-009 — A new site needs its scheduler up before any `queue_action`

**Symptom.** Documents lock permanently; writes return 417 `DocumentLockedError`;
a `.lock` file persists.

**Cause.** A new site in a running bench with no scheduler and no draining queue.
`queue_action` enqueues work that never runs, and the lock is never released.

**Rule.** Confirm scheduler and workers before the first write. Do **not** clear a
recurring `.lock` by hand — it is a regression signal. The root cause of the
earlier variant was per-host database grants; clearing the lock hides that.

## — TRAP-010 — Static routes shadow dynamic ones

**Symptom.** You retire a legacy page so the new template can serve its route,
and the URL 404s.

**Cause.** With a flat `:slug` scheme, a static route wins over the dynamic one.
Both directions of the fix bite: retiring first 404s live URLs, and renaming
breaks inbound links.

**Rule.** Build the template and verify each record renders **before** retiring
any legacy page, one page at a time. Never batch this.

**Also:** two published pages on the *same* route do not conflict — Builder
resolves with `order_by published_at desc, creation desc`, so the most recently
published silently wins and the other becomes unreachable with no error.
`primitives.template.check_routes()` refuses duplicates outright and reports
shadowing, which is usually a deliberate transitional state.

---

## — TRAP-017 — Over REST, a component save locks the document unless a worker is running

**Symptom.** Loading a clone over the Frappe REST API appears to succeed, then a
later write fails with `DocumentLockedError` / HTTP 417. A `.lock` file persists
under `sites/<site>/locks/`. Over `bench` the same load is fine.

**Cause.** `BuilderComponent.on_update` calls `self.queue_action("clear_page_cache")`
unless one of `frappe.flags.in_import`, `in_install`, `in_migrate` or `in_patch`
is set. `queue_action` **locks the document and enqueues a job**; the lock is
released by the job, not by the request. With nothing draining the queue the
lock is permanent.

Loading via `bench` sidestepped this by setting `frappe.flags.in_migrate = True`
in the script it executed. **That flag is not reachable over HTTP** — there is no
whitelisted method to set it, and there should not be. So the transport that
looked equivalent is not: the same payload that loads cleanly through `bench`
locks a component over REST.

**Rule.** Confirm a worker is *actually* draining the queue before the first
write over REST — not that one is configured, that one is beating. This is
TRAP-009's rule with the reason made specific, and it is enforced rather than
documented: `frappe_client.require_worker()` runs before every mutating call and
refuses with a message naming this trap. An unparseable or missing heartbeat
counts as *dead*, because "cannot tell" is exactly the case where guessing
produces the permanent lock.

**Testable:** yes — `tests/test_frappe_client.py::WorkerHeartbeatTest`.

**Related, and worth knowing it is *not* a problem:** the route caches
(`find_page_with_path`, `get_web_pages_with_dynamic_routes`) are `@redis_cache`,
not per-process. `BuilderPage.on_update` calls `clear_route_cache()` whenever
route, published or blocks change, and because the cache is in redis that clear
is visible to every process. So TRAP-015's stale-route 403 does **not** need a
separate `bench clear-cache` when loading over REST. Verified against the pin,
not assumed.

---

## Adding a trap

1. Append an entry here with symptom, rule, and whether it is testable.
2. If testable, add the case to `tests/test_traps.py` in the same commit.
3. If a tool could have prevented it, that tool changes too. The ledger records
   what we learned; the code is what stops it recurring.
