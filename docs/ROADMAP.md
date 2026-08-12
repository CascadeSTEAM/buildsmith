# Bootstrap Plan v3 — Buildsmith, a publishable Frappe Builder design/maintenance project

**Status:** draft for approval · **Date:** 2026-08-03 · supersedes v1 and v2 (same file)
**Name:** `buildsmith` — **decided** (ADR-000)
**Decisions taken:** app + tooling in one repo · full OpsKit-grade governance ·
scope = Builder + go-live orchestration · **private layer stays in Buildsmith** (§2)

> **Genericised for publication, 2026-08-03.** `<site>` placeholders and `TKT-`
> ticket numbers stand in for the real client names, helpdesk prefixes and repo
> names of the first engagement. The mapping is not recorded here and must not
> be — it lives with the operations project.

---

## 0. What changed, and the principle that settles it

**v1** rebuilt about ten things OpsKit already ships, quietly re-introduced
credential handling into the public repo, doubled down on `compose.py` — which the
owner had already ruled was methodology drift — and missed the accumulated Builder
safety knowledge entirely.

**v2** fixed those but overcorrected: it moved Buildsmith's private website data
into OpsKit's `environments/<env>/`. That was wrong (owner, 2026-08-03) and the
argument for it was invalid — it claimed a second private area forces a second
client-token list, when the token list and the data location are independent: the
hook calls OpsKit's guard either way, so the list is singular regardless. Filing
website content in the infra environment organises by *sensitivity* instead of
*ownership*, and it couples Buildsmith to OpsKit even for a demo site with no client
and no infrastructure — which breaks the standalone-publishable goal.

**v3's boundary, and the rule for every future call:**

> **OpsKit owns *actions* against live systems** — access, secrets, deploys, DNS,
> ticketing, and guard-as-a-service.
> **Buildsmith owns *design artifacts*, including its private ones.**

Delegate by **capability**, never by sensitivity. Data lives with its owner.

---

## 1. The two workflows v1 conflated

These share primitives but are **not the same product**, and merging them is what
produced the `compose.py` drift:

| | **W1 · Replicate** | **W2 · Theme & maintain** |
|---|---|---|
| Goal | Spin up an instance and get a **faithful, complete** Builder copy of the client's existing site, fast | Evolve a site's design system and content model over time |
| Owner statement | "productized service… NOT a bespoke redesign tool" (2026-07-22) | TKT-0055 theming overhaul (2026-07-31) |
| Method | full-site crawl → faithful per-page HTML→block conversion → publish all routes | token manifest → components → page template → data-driven records |
| Success floor | all original content present, site navigable, routes preserved | design system coherent, maintenance cheap |
| Reference impl | `builder-site-deploy` skill (`crawl.py`, `create-pages.py`) | `tools/<site>-website/` (`<site>_components.py`, `migrate_content.py`, `build_review.py`) |
| Fate of `compose.py` | **deleted** — it is the drift | its *token-driven* successor is `<site>_components.py` |

**Both must emit a template.** Owner rule, 2026-07-22, no exceptions: every site
build emits design tokens (`Builder Variable`, referenced `var(--uuid, literal)`),
reusable `Builder Component`s (header/footer, pages hold thin references), **and** a
`Builder Page` template (`is_template=1`, `template_group`). Skipping it makes later
maintenance prohibitively expensive.

## 2. The private layer lives in Buildsmith

Website design data is Buildsmith's own data. It stays in Buildsmith, gitignored,
using the same proven mechanism OpsKit uses for `environments/*`:

```
buildsmith/
  sites/example/           COMMITTED — fictional reference, no real domain
  sites/<site>/            GITIGNORED — its own private git repo
    site.yml               target alias, hostnames, Bitwarden ref NAMES only
    brand/                 logos, source palette
    crawl/                 crawled source site (W1)
    content/               entries.json, assets.json (W2)
    tokens-applied.json    live token map read back from the site
    state-export/          Builder state exports for simulate-before-write
    journal/               per-run build journal (§6)
```

`.gitignore`: `sites/*` + `!sites/example/` — byte-for-byte the pattern OpsKit runs
on `environments/`, which has held in daily use.

**No second token list.** The pre-commit hook calls **OpsKit's**
`bin/publication-guard.sh` with Buildsmith as the tree under test; the client-token
list stays anchored to OpsKit (§3). One list, one place to add a client.

**No sync tooling in M1.** One developer, one machine — `sites/<site>/` is a local
private git repo pushed by hand. A `site-sync.sh` (or reuse of `env-sync.sh`) is
built when there is a second machine or a sharing need, not before. `.site-remotes`
does not exist yet.

**Why not OpsKit's `environments/<env>/`** (v2's rejected proposal): it organises by
sensitivity rather than ownership — infra inventory and website copy have different
lifecycles, reviewers and tooling — and it couples Buildsmith to OpsKit even for a
demo site with no client and no infrastructure, which contradicts the
standalone-publishable goal. A public project cannot document its private-layer
story as "put it inside this other private project."

**What secrets policy still applies:** `site.yml` carries **reference names only** —
no credentials, no IPs, no raw hostnames beyond the public ones. Secret *resolution*
is an action, so it stays OpsKit's (`bin/bw-management.py`).

## 3. Delegation table — actions Buildsmith does NOT implement

Everything below is an **action against a live system**, or a guard/ledger OpsKit
already owns. Buildsmith calls these; it never reimplements them.

| Need | Already in OpsKit — use this | v1 mistake |
|---|---|---|
| Token/IP/message publication guard | **`bin/publication-guard.sh`** (`--cached`, range, `--messages`; `CLIENT_TOKENS` env for CI so the list stays unpublished) | proposed writing `publish-check.sh` |
| Git hook install | `buildsmith hooks`, `.githooks/` | proposed copying both |
| Client token list | OpsKit `.client-tokens` (+ `CLIENT_TOKENS`) — **single list, anchored to OpsKit** | proposed a second list |
| Secret resolution | `bin/bw-management.py` (Vaultwarden) | proposed `--api-key/--api-secret` flags |
| Frappe API access (Path A) | `mcp/erpnext-mcp-server.py` — token-auth service account | proposed a new MCP server |
| Frappe admin exec (Path B) | `bin/frappe-exec.py` + **`frappe-access` skill** routing rule | proposed REST publishing with keys |
| Site/bench provisioning, upgrades | `erp_stack`, `frappe-bench`, `frappe-site`, `frappe-upgrade` roles | — |
| DNS + reverse proxy | `configure-cloudflare-dns` playbook, `caddy` role (incl. `upstream_host`) | proposed porting `deploy-dns.sh` |
| Ticketing | `bin/open-ticket.sh`, `helpdesk-ticket` skill | — |
| Idea capture / automation ladder / DoD | `bin/idea.py`, `bin/automation-ladder.py`, `bin/definition-of-done-guard.py` | — |
| Session notes routing, endsession | OpsKit + DocWright `endsession` | proposed a new mechanism |

**Not delegated — Buildsmith's own:** the private site layer (§2), design artifacts,
the trap ledger, simulate, docgen, the journal, the sandbox.

**The one seam that needs work, and it's an OpsKit PR, not a copy:**
`publication-guard.sh` resolves both the repo under test *and* the token sources
from `OPSKIT_ROOT`. To guard a foreign repo it needs those separated — add
`--repo <path>` (tree under test) while token collection stays anchored to the
OpsKit root. Small, testable, benefits OpsKit itself. **File it as an OpsKit issue
in Phase 0**; until it merges, buildsmith's hook calls the guard with
`CLIENT_TOKENS` exported from OpsKit's list (already supported).

**Fail closed:** buildsmith's `pre-commit` **refuses to commit** if it cannot
locate the OpsKit guard, unless `BUILDSMITH_PUBLIC_ONLY=1` (for CI on the public
repo, where no client tokens exist to leak).

### Deliberate exception: the local sandbox stays in-repo
OpsKit's IaC rule would route local provisioning to an Ansible playbook against the
`workstations` group. buildsmith keeps `sandbox/docker-compose.yml` in-repo
anyway, because **a publishable project must be runnable by someone who does not
have OpsKit.** The boundary is crisp and goes in `AGENTS.md`: *local disposable
containers → in-repo compose; anything touching a server → OpsKit.*

## 4. The Builder version reality — this reshapes the roadmap

> **Corrected 2026-08-03 — see ADR-004.** This section as originally written was
> wrong in its central claim, and the correction is preserved here rather than
> edited away, because the *way* it was wrong is the lesson: it treated a
> reported version string as if it identified a version.
>
> `1.0.0-dev` is the placeholder `__version__` on Builder's `develop` branch, set
> 2025-12-12 and unchanged across 1000+ commits since. Consequences:
>
> - **The round-trip is not absent.** `export_import_standard_page.py` was added
>   2025-11-17, *before* the version reset — every checkout reporting
>   `1.0.0-dev` has it. The gating below is withdrawn.
> - **`Builder Variable` was renamed to `Builder Token`** on 2026-07-05
>   (`f0781da9`), inside that same version string. Half the trap ledger names a
>   doctype that may or may not exist depending on the commit (TRAP-011).
> - **The pin must be a SHA.** `buildsmith sandbox up` refuses anything else.
>
> The capability list below is still accurate — but as a description of a
> *commit*, not of a version. The provisional pin and its derivation are in
> ADR-004; confirming it against the target is Job A.

Target is **Builder v1.0.0-dev** (per the accumulated notes). The `builder_files/`
standard-page export/import — the whole basis of v1's app layer — is a **later
upstream feature and almost certainly absent there**. v1's fallback ("build the app
layer anyway") would have passed a sandbox test and delivered nothing.

**What v1.0.0-dev *does* have** (verified the hard way, per the trap ledger):
`is_template` + `template_group`, `Builder Variable` (Color/Dimension + `dark_value`),
`Builder Component` (`autoname: field:component_id`), repeaters + `dataKey`,
`Builder Snapshot`, `extend_block()` shell semantics.

**Therefore:**
- **M1 delivers value on v1.0.0-dev**, using tokens + components + template +
  data-driven pages — the TKT-0055 stack, which already works.
- ~~**The `builder_files/` round-trip is M3, gated on a Builder upgrade**~~
  — withdrawn, ADR-004. It is present at the pin. Whether M3 is worth doing is
  now a question of merit, not availability.
- **The sandbox pins the target's Builder commit**, so sandbox results are honest.
  A second pin tracks upstream `develop` for evaluating the upgrade.

## 5. Repository layout

> Rewritten 2026-08-06 to match the tree as it exists. The original section
> showed the pre-ADR-006 plan — a Makefile, `bin/`, top-level `primitives/` —
> and promised docs (`workflows/theme.md`, `site.yml.schema.md`) that were
> never written. A layout diagram that disagrees with `ls` is the same defect
> class as a stale version string.

```
buildsmith/                       public except sites/* (§2)
├── AGENTS.md                       the working guide (CLAUDE.md is a pointer)
├── LICENSE  pyproject.toml         MIT (§9); zero runtime dependencies
├── install.py                      stdlib-only bootstrap (runs pre-install)
│
├── buildsmith/                     the one package (ADR-006: no Make, no bin/)
│   ├── cli.py                      the entry point; every subcommand routes here
│   ├── errors.py                   CouldNotCheck + the 0/1/2 exit contract
│   ├── primitives/                 tokens, blocks, components, repeater, template
│   ├── workflows/                  replicate/ (W1) · theme/ (W2) · optimize/ (W3)
│   └── tools/                      guard, hooks, sandbox, simulate, validate,
│                                   docgen, journal, clone_diff, visual_check,
│                                   drift, audit, adopt, capture_dev, golive, …
│
├── sandbox/                        pinned Builder bench (compose + pins.env + init.sh)
├── skills/                         agent surface (§8)
├── docs/
│   ├── catalog.md                   GENERATED — stale copies fail pre-commit
│   ├── builder-schema.md            GENERATED from the pinned Builder
│   ├── traps.md                     ledger, each entry with a test (§7)
│   ├── workflows/replicate.md
│   ├── workflows/data-driven-pages.md
│   └── decisions/NNN-*.md           ADRs
├── sites/example/                   COMMITTED fictional fixture (no real domain)
├── sites/<site>/                    GITIGNORED — private site layer (§2)
└── tests/
```

Nothing here reaches a live site. **Every tool reads files and writes files.** The
`tools/<site>-website/README.md` discipline, verbatim: *"Nothing here touches a live site
— these scripts emit data, and an OpsKit subagent applies it."* v1 regressed this;
v2 makes it a structural property (no HTTP client is a dependency of any tool).

## 6. Self-documentation, designed in rather than promised

Four mechanisms, each **enforced** so docs cannot rot:

1. **Generated catalog.** `buildsmith docs` renders `docs/catalog.md` from emitted
   token manifests, component payloads and template groups — every component with
   its props, tokens consumed, and a rendered preview. **Pre-commit fails** if
   `primitives/` or a payload changed and `docs/catalog.md` wasn't regenerated. The
   component library documents itself as a side effect of existing.
2. **Generated schema reference.** `docs/builder-schema.md` is introspected from the
   *pinned* Builder in the sandbox (doctype JSON + `builder_page.py` render rules),
   never hand-maintained. Regenerating it is how a version bump gets reviewed.
3. **Trap ledger with teeth.** `docs/traps.md` — every hard-won gotcha as
   `TRAP-NNN` with symptom, rule, and, where expressible, a **test in
   `tests/test_traps.py`** asserting our tooling cannot commit it. Docs that fail
   the build. Seeded from the existing knowledge (§10).
4. **Run journal.** Every tool run appends a JSON record (inputs, Builder pin,
   token map hash, counts, outputs, warnings) to the site's private
   `journal/`; `buildsmith journal render` turns it into the build log for the
   helpdesk ticket. This is what makes a site maintainable a year later — and it
   lives in the private layer, so no facts leak.

Plus: ADRs in `docs/decisions/` for name, licence, Builder pin, the W1/W2 split.
Session notes and endsession **reuse OpsKit + DocWright** — no new mechanism.

## 7. Simulate before write — the tool that pays for the project

The most expensive near-miss on record: replacing an in-use component's `block`
with a freshly-composed tree would have **wiped the header and footer across all 13
pages** of the live site. It was caught only because a subagent simulated first.

`buildsmith simulate` makes that structural: given a payload + a Builder **state
export**, it reproduces `extend_block()`'s shell matching (`blockId` /
`referenceBlockId`) and reports, per affected page, which interior nodes would
collapse to `element=None`. **Non-zero exit on any collapse.** No component payload
is handed to OpsKit without a clean simulate. This is `TRAP-001` and it gets a test.

## 8. Skills (agent surface)

- `builder-replicate` — W1 end to end; full-site crawl, faithful conversion, all
  routes, template emitted, publish handed to OpsKit.
- `builder-theme` — W2; token manifest → components → template → data-driven pages,
  with the TKT-0055 step ordering and its shadowing constraint.
- `builder-safety` — loaded before **any** component or page write: traps, simulate,
  snapshot-first, developer_mode dance.
- `builder-golive` — choreography only; generates the command plan + verification
  checklist, execution via OpsKit (`configure-cloudflare-dns`, `caddy`, Frappe domains).

Each skill's first instruction is to load OpsKit's `frappe-access` skill for the
access-path decision. No MCP server in v1 — OpsKit's Path A/B already covers it, and
OpsKit's own automation-ladder rule says a tool earns an MCP surface only after the
manual path has been used ~3 times.

## 9. Decisions closed in v2 (no longer open items)

- **Licence: MIT.** We ship no Builder-derived code — the block schema is data, and
  `builder_templates/` fixtures are our own authored content. Builder's AGPL-3.0
  would only bind us if we vendored its source, which we don't.
- **No MCP server in v1** (above).
- **developer_mode correction.** v1 claimed the dev-mode requirement "structurally
  prevents designing against production." **Wrong** — setting `is_template=1` over
  REST *requires* developer_mode **on the live site**: enable → publish the template
  → disable. It is per-site and contained, and it belongs in `builder-safety` as a
  procedure, not an impossibility.

## 10. Knowledge to port from the predecessor repo (the primary source)

Port as the seed of `docs/traps.md`, `primitives/`, and the W2 workflow:

**Code** — `tools/<site>-website/`: `<site>_components.py` (token-driven composer, zero
hardcoded colours, `var(--uuid, literal)` with the literal as fallback),
`migrate_content.py` (Obsidian `![[embeds]]`/`[[wikilinks]]` resolved against a
route index, `|image-right` portraits, HTML-commented drafts stripped, non-zero exit
on route collisions), `build_review.py` (review artifact generated from *actual*
composer output, not a hand-written approximation — keep this discipline),
`website-entry-doctype.json` (the data-driven page model), `<site>-tokens.json` (manifest
*intent* vs applied *result* — keep that distinction).
From the skill: `crawl.py` (content only, never JS) and `create-pages.py`'s
asset-upload/upsert machinery. **`compose.py` is not ported.**

**Docs** — `docs/<site>-website-design-system.md`, `docs/<site>-website-content-model.md`,
`docs/website-backup-and-export.md`. Client-specific content goes to the private
layer; their **structure** becomes the generic template.

**Traps** (seed `docs/traps.md`; ★ = testable):
- ★ `TRAP-001` component shell / `referenceBlockId` / `extend_block` collapse;
  `on_update` does not `sync_component()` → preserve blockIds and restyle in place,
  or run `ComponentSyncer` across every consuming page.
- `TRAP-002` a composed component is a structural skeleton — it carries no content,
  so swapping one in drops nav links, addresses, everything.
- ★ `TRAP-003` repeaters, six silent-failure rules: needs `isRepeaterBlock` **and**
  `children` **and** `dataKey`; only `children[0]` repeats; the loop iterates
  `dataKey.key` (`property` on the container binds the container); `src`/`href` need
  `type: "attribute"`; the same binding in both `dataKey` and `dynamicValues` leaks
  raw Jinja; `visibilityCondition` is not evaluated on a repeater's immediate child.
- ★ `TRAP-004` `Builder Variable.type` is exactly `Color` | `Dimension`. Font
  families, weights, unitless line-heights, shadows, easing **cannot** be tokens →
  component props + one injected `head_html` stylesheet. `dark_value` is on the same
  record; differing values emit `light-dark()`.
- ★ `TRAP-005` `Builder Component` is `autoname: field:component_id`; Frappe
  force-syncs the field to `name` — pages reference `name`, `clear_page_cache()`
  matches `component_id`; they must not diverge.
- `TRAP-006` `is_template=1` needs developer_mode **on the live site** (enable →
  publish → disable).
- ★ `TRAP-007` never delete an existing `Builder Variable` — one page alone held 50
  `var(--uuid)` references. Rename/remap in place.
- `TRAP-008` timezone: `System Settings.time_zone` blank ⇒ IST storage; a
  `creation` of `2026-08-01 07:09` is `2026-07-31 18:39 PDT`. This already caused a
  stale probe to be trusted. Convert before concluding; settle the timezone before
  creating dated records.
- `TRAP-009` a new site in a running bench needs its scheduler up / queue draining,
  or `queue_action` locks docs forever (417 `DocumentLockedError`). Root cause of the
  earlier variant was per-host MariaDB grants (TKT-0061); a recurring `.lock` is a
  regression signal, not something to clear by hand.
- `TRAP-010` static routes shadow dynamic ones — with a flat `:slug` scheme,
  build the template and verify each record renders **before** retiring the legacy
  page, one at a time. Retiring first 404s live URLs; renaming breaks inbound links.

**Also port:** backup + Builder Snapshot before any run (the safety net that made
"watch it land live" an acceptable posture), and the *intent vs applied* pattern —
the manifest is design intent, the applied map is live state read back, and the two
are never the same file.

## 11. Milestones (3, not 8)

### M1 — A public repo that can design, on the version we actually run
> **Status: complete, 2026-08-04.** All four steps done and committed; every
> exit criterion met.
1. `git init` fresh at `~/Projects/buildsmith` (no history import — OpsKit still
   carries `.pre-scrub-backup-*.bundle` from that mistake). Write ADR-000 (name),
   ADR-001 (MIT), ADR-002 (actions-vs-artifacts boundary, §0), ADR-003 (Builder
   pin). Repo **private** until the guard passes.
2. Governance by **delegation**: `.gitignore` (`sites/*` + `!sites/example/`), hooks
   calling OpsKit's `publication-guard.sh` with Buildsmith as the tree under test,
   fail-closed wrapper, OpsKit issue for `--repo`. **Exit: poison-test commit —
   fake client name, a 10.x address, and a staged `sites/<site>/` path — is
   rejected on all three counts.**
3. `sandbox/` pinned to the target's Builder commit. **Exit: sandbox reproduces a
   known trap** (e.g. a repeater misconfigured per TRAP-003 renders as a normal
   block) — proving the sandbox is a faithful test bed, not just a running app.
4. Port `primitives/` + W2 from `tools/<site>-website/`, with `docs/traps.md`,
   `tests/test_traps.py`, `buildsmith simulate`, `buildsmith docs`, `buildsmith journal`.
   **Exit: `buildsmith test` green; `docs/catalog.md` generated; simulate catches a
   deliberately-broken component payload.**

### M2 — Replicate workflow + first real use
> **Status: step 5 done, step 6 half done (skills shipped).** The rest of
> step 6 needs a live client engagement, and step 7 touches the predecessor
> repo, so both want a human in the loop.
5. W1: full-site crawl (today's `scrape.sh` does one page), faithful HTML→block
   conversion, all routes, mandatory template emission.
6. Skills + `AGENTS.md`. Drive **TKT-0055 step 5/6** through the new repo as the
   first real consumer — it is the honest acceptance test.
7. Cut over from the predecessor repo: move `tools/<site>-website/` + `builder-site-deploy`;
   **keep** image building, `use-cases/`, `Containerfile`, the `erp-images` plugin.
   Update that repo's `AGENTS.md` to point here.

### M3 — Round-trip + publication
> **Status: step 8 evaluated (ADR-005), step 9 done.** The round trip carries
> pages and components but silently drops tokens (TRAP-013), so building the app
> layer on it is now an informed decision rather than a blocked one. Step 10
> (flip public) is the owner's call.
8. Builder upgrade evaluated (OpsKit ticket, `frappe-upgrade` role). If it lands:
   `builder_files/` + `builder_templates/` app layer, with the round-trip proof
   (author in sandbox → JSON in git → `bench migrate` on a second site reproduces it).
9. Go-live choreography as a generated plan + checklist; execution via OpsKit.
10. `publication-guard.sh` over full history + human read-through for *facts* →
    flip public.

## 12. Gates — both now clear

- **ADR-000 name: `buildsmith`.** Decided (owner, 2026-08-03). It becomes the Frappe
  app module, baked into `hooks.py`, every exported page's `project_folder`, and
  `builder_files/` paths — so it does not change again.
- **§2 private layer: stays in Buildsmith.** Decided (owner, 2026-08-03).

Nothing blocks M1 step 1. The Builder-version check (§4) and the OpsKit `--repo`
issue both need an OpsKit subagent and can run in parallel with M1 steps 1–2.

## 13. Remaining risks

| Risk | Handling |
|---|---|
| Builder upgrade never happens | M1+M2 deliver full value without it; M3 is genuinely optional. |
| `publication-guard.sh --repo` PR stalls | `CLIENT_TOKENS` env path works today; no blocker. |
| W1 faithfulness on SPA/Quartz sources | Known exception, documented, not a defect. Static/marketing sites replicate well. |
| Upstream ships competing AI page generation | Our value is the git-tracked, reviewable, multi-site, trap-aware workflow — not prompt-to-page. Don't duplicate it. |
| A `sites/<site>/` file gets staged by accident | `.gitignore` (OpsKit's proven pattern) plus OpsKit's guard, which checks staged **paths** as well as content. Poison-test covers it. |
