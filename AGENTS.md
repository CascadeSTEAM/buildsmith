# AGENTS.md — Buildsmith

Buildsmith designs and maintains **Frappe Builder** websites: design tokens,
components, page templates, data-driven pages, and full-site replication. It is
intended to be **published publicly**, so it must stay free of client-identifying
information.

## START HERE, every session
1. **`docs/ROADMAP.md`** and **`docs/ISSUES.md`** — the plan and the open items:
   what is done, what is next. (`RESUME.md`, if present, is a gitignored private
   working file — the bootstrap-era narrative; public clones do not have it.)
2. **`docs/ROADMAP.md`** — the plan (v3). §0 boundary, §2 private layer,
   §3 delegation, §4 Builder-version reality, §11 milestones.
3. **`docs/traps.md`** — hard-won Builder failure modes. Load it before composing
   or writing **any** component or page. Several of these have already cost real
   damage on a live site.
4. **`docs/decisions/`** — ADRs. ADR-004 in particular, because it invalidates a
   claim the roadmap still states in its own voice.

## The standing boundary (the rule that settles most questions)

> **OpsKit owns *actions* against live systems** — access, secrets, deploys, DNS,
> ticketing, and guard-as-a-service.
> **Buildsmith owns *design artifacts*, including its private ones.**

**Delegate by capability, never by sensitivity.** Data lives with its owner. Filing
something in another project because it happens to be private is the mistake this
rule exists to prevent.

Corollary: Buildsmith must remain usable **standalone** by someone who has no
OpsKit — e.g. for a demo site with no client and no infrastructure.

## Hard rule — live infrastructure goes through an OpsKit subagent

Any access to a live system is performed by a **subagent operating in the OpsKit
project** (`~/Projects/opskit`) — never directly from this repo. This includes SSH
into hosts, Frappe bench operations, app/package installs, DNS changes, reverse-proxy
edits, secret retrieval, and any write against a live Frappe instance.
**Offer this routing proactively** whenever such work comes up.

Before running code against any Frappe site, load OpsKit's **`frappe-access`** skill
and take the path it dictates: **Path A** (`mcp/erpnext-mcp-server.py`, HTTP/API) by
default; **Path B** (`bin/frappe-exec.py`) only when the API is unavailable or the
operation genuinely needs admin. Never hand-roll `ssh … docker exec … bench console`.

## Tools emit files — they never touch a live site

Almost every tool in this repo **reads files and writes files.** The exception is
loading into the *local dev instance*, and it is bounded by construction:

**No tool here can reach a site outside `LOCAL_ONLY`** (`sandbox.localhost`,
`roundtrip.localhost`). There is no override flag, on purpose — an override is
how "local only" becomes "local by default". Publishing to anything real means
handing the emitted payload to an OpsKit subagent to apply. This is a structural
property, not a convention — keep it that way.

Exactly one module can speak HTTP to a Frappe instance:
`buildsmith/tools/frappe_client.py`. Three gates run before any write — site
name, host shape, and a live worker (TRAP-017). Credentials are **read, never
resolved**; Buildsmith has no secret-store client and must not grow one, which
`tests/test_frappe_client.py` enforces by inspecting the module's imports.

The invariant used to be phrased as "no tool opens a socket." That described the
transport, not the guarantee, and it could not survive the container
architecture. See `docs/decisions/007-the-guarantee-is-the-target.md`.

## The private layer

`sites/<site>/` is **gitignored** (only `sites/example/` is committed) and holds
per-site private data: brand assets, crawls, content, the applied token map, Builder
state exports, and the run journal. `site.yml` carries **reference names only** —
no credentials, no private IPs. Secret *resolution* is an action, so it is OpsKit's.

## Publication guard

The pre-commit hook delegates to OpsKit's `bin/publication-guard.sh` (single client-
token list, anchored to OpsKit) plus a Buildsmith-local check that refuses staged
paths under `sites/` other than `sites/example/`. If the guard cannot be located or
its token list comes back empty, commits **fail closed**. Never disable the guard to
get a file committed — genericise the file instead.

**Generic credentials are a separate scanner** (ADR-010): gitleaks runs beside the
guard — staged diff at pre-commit, full history at pre-push and in CI — because the
guard's patterns were never taught what an API key or a private-key block looks
like. A missing gitleaks binary refuses the commit (exit 2, could not check).
Reviewed false positives are allowlisted in `.gitleaks.toml`, in a commit, never
via the skip variable. Forge-side setup (branch ruleset, GitHub secret scanning,
CI secret): `docs/going-public.md`.

Nothing client-identifying goes in tracked files, commit messages, branch names, or
issues. Note that **facts leak too**: a token-free sentence describing what runs
where is still not publishable.

## Two workflows — do not merge them

- **W1 · Replicate** — faithful, complete copy of a client's existing site into
  Builder, routes preserved. A productized service, *not* a redesign tool.
- **W2 · Theme & maintain** — token manifest → components → page template →
  data-driven pages.

`compose.py` from the predecessor repo is **not ported**: imposing a curated,
opinionated design was ruled methodology drift.

**Every site build must emit a template** — design tokens (`Builder Variable`),
reusable components (header/footer, pages holding thin references), **and** a
`Builder Page` template (`is_template=1`, `template_group`). No exceptions; skipping
it makes later maintenance prohibitively expensive.

## Never trust a reported version string

Builder's `develop` branch has reported `__version__ = "1.0.0-dev"` since
2025-12-12, across more than a thousand commits. Inside that unchanged string it
added the `Builder Snapshot` doctype and **renamed `Builder Variable` to
`Builder Token`** — a doctype most of our tooling is built on.

So: **pin by commit SHA, and check capabilities against the pin, not against a
version.** `buildsmith sandbox up` refuses a branch or tag. Full evidence in ADR-004;
the failure mode it records — reading a version string as if it identified a
version — is the kind of mistake this project keeps making, so read it before
concluding anything about what Builder can do.

## Skills — the agent surface

Load `builder-safety` before **any** component or page write. It is not optional
and it is not "if this looks risky".

| skill | when |
|---|---|
| `builder-safety` | before any Builder write — traps, simulate, snapshot, developer_mode |
| `builder-theme` | W2: token manifest → components → template → data-driven pages |
| `builder-replicate` | W1: faithful full-site copy, routes preserved |
| `builder-golive` | go-live choreography; execution belongs to OpsKit |
| `builder-optimize` | W3: builderize a site in the sandbox — baseline, transform, oracle (ADR-009) |
| `dogfood-cycle` | run it for real, fail, fix, retry until MVP; as a release phase, log-don't-fix (ADR-011) |
| `plow` | batch-clear the GitHub backlog — PR queue first, then dedupe, prioritize, and work issues one at a time through the improvement cycle |

Every skill whose work reaches a Frappe site starts by loading OpsKit's
`frappe-access` skill for the access-path decision; process skills (`plow`,
`dogfood-cycle`) instead route any live-system need to an OpsKit subagent. A new
skill is registered in this table in the same commit that adds it —
`tests/test_skills.py` fails otherwise.

## The CLI is the interface

`buildsmith` (argparse, stdlib only) is the sole entry point — there is no
Make and no `bin/` (ADR-006). The eventual container entrypoint and TUI go
through the same CLI: one implementation, so the surfaces cannot drift.

Everything lives under the `buildsmith.` namespace because `primitives`,
`workflows` and `content` are all **taken on PyPI**; a published package
claiming names that generic would collide.

**Exit codes mean something:** `0` proved · `1` found a problem · `2` could not
check. The third is separate on purpose — "I could not verify this" must never
read as "this is fine". `buildsmith verify` returns 2, not 0, when playwright is
absent.

## Tools

| command | does |
|---|---|
| `buildsmith test` | unit tests, poison test, generated-docs check |
| `buildsmith secretscan [--history]` | generic credential scan via gitleaks (ADR-010) |
| `buildsmith build --site <s>` | emit payloads from a site's design inputs |
| `buildsmith validate` | validate emitted payloads |
| `buildsmith handoff` | validate, then print the operations handoff brief |
| `buildsmith docs` | regenerate `docs/catalog.md` |
| `buildsmith sandbox up/status/serve/destroy` | the pinned Builder test bed |
| `buildsmith check traps/simulate/roundtrip` | prove the sandbox still matches the pin |
| `buildsmith simulate` | dry-run a component payload against a state export |
| `buildsmith journal` | append to / render the run journal |
| `buildsmith golive --site <s>` | generate the go-live plan from the actual build |
| `buildsmith clone --site <s> --source <url>` | crawl, convert, extract `features.json`, and load into the dev instance (`--no-load` to skip) |
| `buildsmith load --site <s>` | load an already-emitted `build/` payload into the dev instance — the deferred counterpart to `clone --no-load` |
| `buildsmith adopt --site <s>` | load a live export into the sandbox exactly (ADR-008 Maintain) |
| `buildsmith verify --site <s> [--source <url>]` | content diff (set differences, not counts) plus a browser check against `features.json` |
| `buildsmith optimize status --site <s> [--json]` | W3: where am I in the pipeline — baseline, gate ledger, proposals (artifacts only) |
| `buildsmith optimize baseline/oracle --site <s>` | W3: capture the reference; prove rendering unchanged (ADR-009) |
| `buildsmith optimize tokenize/fonts/collapse/componentize --site <s> [--apply]` | W3 Phase A transforms: mine proposals, apply accepted ones; every apply ends in the oracle |

## Validate by content, never by counts

Three features — a hover menu, a lightbox, a hero background — survived this
project's own "the clone matches" verdict, because that verdict compared
declaration counts and byte sizes. **375 of 376 declarations can be 375 different
declarations.** Every one of those omissions was found by a human looking at the
page.

So: counts are a cheap tripwire, `buildsmith verify`'s content diff is the verdict, and
its browser check is the only thing that can tell you a *feature* works —
it performs the click and the hover and asserts the page changed. A handler that
binds and does nothing passes every static check ever written.

`sites/<site>/features.json` is **extracted from the source, never written by
hand**, because a hand-written checklist contains only the features somebody
remembered and the problem is always the ones nobody noticed.

## Self-documentation is enforced, not optional
Generated catalog and schema reference, a trap ledger with tests, and a per-run
journal. If a change makes generated docs stale, the commit fails. See
`docs/ROADMAP.md` §6.
