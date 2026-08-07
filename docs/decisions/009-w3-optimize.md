# ADR-009 — W3 Optimize: two phases with opposite proofs

**Status:** accepted · 2026-08-05 · owner's direction; draft critiqued by a
fresh-eyes review (10 findings, all folded in) before acceptance.
**Builds on** ADR-008 (two cloning scenarios). Settles the four plan questions
the bootstrap session notes left open.

## Context

ADR-008 named two ways a site arrives: **Import** (non-Frappe source, crawled
and converted) and **Maintain** (already Builder, records copied exactly). Both
end with Builder records in the sandbox — and both can end with records that
are *on* Builder without being *of* it.

The adopted dogfood site is the type specimen, measured from its live export
(customer pages only — most pages in the export are Builder's shipped
templates, and those score properly on every axis). The shape, deliberately
bucketed here (facts leak too; exact figures live in the private layer):

- a handful of pages, ~850 blocks, ~80% of them on a single page;
- element soup: the large majority of blocks are bare `div`/`span`;
- **zero** design-token references — hundreds of literal colour uses drawn
  from a **single-digit distinct palette**;
- hundreds of `fontFamily` declarations drawn from **two distinct stacks**,
  stacks the Builder editor cannot parse (the editor is broken on live today);
- responsive styles on under 1% of blocks; one component in use of the dozens
  defined; a couple dozen client scripts; no page template.

That is the point: the *distinct* vocabulary is tiny but it is applied
literally, hundreds of times. A brand-colour change today is hundreds of hand
edits.

The owner's direction, recorded verbatim in intent:

1. Optimising such a site **is a workflow** (W3), not a one-off — human
   collaboration assisted by AI suggestions and demonstrations, iterating over
   style, components and layout, comparing against similar sites and top-rated
   designs.
2. The import scrape stays an **ephemeral** best-effort collection; the real
   deliverable is the builder-optimised site built from it in the sandbox. The
   current dogfood course: completely redo the adopted site in the sandbox.
   The next course: scrape a fresh non-Builder site end to end.
3. Builder-capability skills are built **as we go** for now; a systematic
   sweep of the whole surface may follow as its own effort. Either way every
   capability is a CLI subcommand with a thin skill wrapper — dual-usage
   (agent-invocable and TUI-callable), never logic living in a SKILL.md.
4. Publish-back (merge vs overwrite against live) is **deferred** until this
   dogfood course ends, and will be decided on its evidence.

## Decision

W3 Optimize is one workflow with **two phases whose proofs point in opposite
directions**. The split exists because the bootstrap notes' acceptance question was
right: "looks the same but uses tokens" is checkable and "POPs" is not — so
the workflow is factored such that everything machine-checkable lands in
phase A and everything judgement-shaped lands in phase B.

### The equivalence oracle — named first, because every Phase A gate hangs on it

"Changed nothing visible" is proven by a **rendering oracle**: per published
route, per breakpoint, a browser screenshot pixel-diff against the baseline
with an explicit threshold, exit 1 on breach — optionally corroborated by a
computed-style diff per matched DOM node. Today's tools do not carry this:
`verify`'s visual check writes screenshots but computes **no** comparison, and
`clone-diff` compares content *sets*, not layout. The oracle is therefore
transform zero's deliverable, and until it exists no Phase A transform may
claim the machine gate.

In Phase A, `clone-diff` is scoped to what it can honestly check across a
structural rewrite: text, links, assets and scripts. CSS-set identity is *not*
part of the Phase A contract — collapse and componentize necessarily re-mint
style bundles, so set differences there are expected; the oracle is the
equivalence relation.

### Phase A — mechanical builderization. Must prove it changed *nothing visible*.

Transforms, in order:

0. **Baseline** — capture the pre-optimization reference from the sandbox:
   crawl → `features.json` (extracted, never hand-written), per-route ×
   per-breakpoint screenshots, a state-export checkpoint, **and the
   script-dependency scan** — which selectors and minted `fb-*` classes each
   client script touches. The scan lives here because collapse and
   componentize are the transforms that re-mint those classes; they consult
   it to refuse or flag a merge that would break a script. Re-runnable
   after each accepted transform, so every step diffs against the last good
   state and any step can be rolled back by re-loading its checkpoint.
   (`Builder Snapshot` may exist at the pin; it is verified against the pin
   before anything relies on it, and the checkpoint mechanism above works
   without it.)
1. **Tokenize** — mine literal **colours** (and dimensions where they repeat)
   from the blocks, emit a proposed token manifest (the human names the
   tokens; the tool only proves coverage), apply it as site-owned token
   records — the doctype name comes from the pin-selected constant in
   `primitives/tokens.py`, `Color`/`Dimension` types only (TRAP-004) — and
   rewrite styles to `var(--<uuid>)` with the original literal as fallback.
   **Because the fallback renders identically whether the token resolves or
   dangles, the gate includes a resolution assertion**: every referenced UUID
   must appear in the served variables stylesheet (equivalently: a probe
   render with fallbacks stripped must not change), so a dead token layer
   cannot pass as a working one (TRAP-013's failure shape).
2. **Collapse** — merge redundant single-child wrappers where the composed
   styles are provably equivalent; every merge is logged with its style proof.
   Runs **before** componentize: it normalizes near-identical subtrees (which
   improves repeated-structure detection) and never has to reason about
   component boundaries — which it must **never** cross once they exist,
   because merging across an `extend_block()` shell breaks TRAP-001's
   blockId matching.
3. **Componentize** — detect repeated subtrees across and within pages (the
   dominant page's repeated item structures; header/footer), and emit
   **proposal files with a status field**; the transform consumes only
   proposals marked accepted, and each decision lands in the run journal.
   That persistence is what makes the workflow deterministic and reusable on
   the next site. Revising the one already-in-use component is a TRAP-001
   revision and goes through `revise()`.
4. **Template & route hygiene** — the mandatory `Builder Page` template
   (AGENTS.md rule: every build emits one) and TRAP-010 route-collision
   checks, including the draft page.
5. **Scripts & leftovers disposition** — using baseline's dependency scan,
   confirm every client script still binds and fires (each script's observable
   behaviour must be represented in `features.json`, so `verify` exercises
   it). Unused components get an explicit prune-or-keep decision with its own
   proposal file — pruning is destructive and is never implied.

**Gate for every transform:** the rendering oracle (all routes × breakpoints
within threshold) · `verify` against `features.json` · Phase-A-scoped
`clone-diff` (text/links/assets/scripts equal) · `simulate` + id-preservation
(TRAP-001) on anything touching components or pages · `conformance` clean ·
scorecard re-measured with hard floors (0 unreviewed literal colours, template
present, no dangling token references). A Phase A transform that changes
rendering is a defect by definition.

**Fonts are the named exception.** TRAP-004 means font families are not token
records — they are a component prop plus a `head_html` stylesheet — and
reducing an unparseable stack to a single family **is a visible change** on
any machine where the first font is unavailable. So font normalization is its
own transform with **reviewed-visible** semantics: the machine shows the
before/after render pair, a human signs it off. It repairs the broken editor;
it does not pretend to be invisible.

### Phase B — collaborative improvement ("POP"). Must change what's visible, deliberately.

The AI proposes; the sandbox demonstrates; the owner adjudicates. Each
iteration is one reviewable proposal — a restyle, a layout change, a
responsive treatment — rendered in the sandbox next to its comparison
evidence. **For this course, comparison evidence (similar sites, top-rated
designs) is hand-supplied by the owner; a collection capability is explicitly
deferred** — third-party captures raise publication-guard and provenance
questions that deserve their own decision.

**Gate, human per iteration, machine on invariants:** the owner accepts or
rejects what it looks like; the tooling enforces that every accepted change is
expressed in Phase A's vocabulary — tokens and components, never new literals
(`conformance`), no block-id loss (TRAP-001 / `simulate`), no route breakage,
and the oracle re-baselines after each acceptance so drift is always relative
to the last approved state.

**Responsive design belongs to Phase B.** The source site has no mobile design
to preserve — inventing one is a visible change by definition. Its gate is
split: desktop rendering unchanged (oracle), mobile rendering reviewed
(human).

Phase B is driven as a skill, but the skill stays thin: proposal generation,
render-pair production, and accept/reject bookkeeping are expected to become
subcommands as they stabilise; the skill only orchestrates the conversation.

### Composition — what makes it a tool and not a one-off

W3 takes Builder records in the sandbox and does not care how they got there:

- **adopt → optimize** (Maintain, then improve) — this dogfood course.
- **import → optimize** (W1's ephemeral scrape, then improve) — the next one.

One optimizer, two feeders. Phase A's transforms are record-to-record
functions over the same primitives (`blocks.py`, `tokens.py`,
`components.py`, `template.py`), so they compose with everything those
already enforce.

### Delivery shape

Each Phase A transform is a CLI subcommand emitting artifacts (payloads plus a
findings report), gated like everything else in this repo; each ships with its
thin skill wrapper in the same commit (`tests/test_skills.py` already enforces
registration).

## Consequences

- The dogfood scorecard (exact before/after numbers) lives in the private
  layer with the site's journal; this ADR keeps only the bucketed shape.
- `clone-diff` findings change meaning by phase: in Phase A the scoped diff
  (text/links/assets/scripts) is a defect detector; in Phase B differences are
  the deliverable and the diff is the review artifact. Same tool, contract
  chosen by phase — ADR-008's lesson applied inside one workflow.
- Publish-back stays undesigned until the course ends (owner's call). Until
  then W3's output stops at sandbox + emitted payloads + handoff brief, which
  the existing OpsKit boundary already covers.
- The Builder capability library grows as-we-go: each W3 need that Builder
  already serves (snapshots, variables, repeaters, breakpoints, …) gets read
  from the pinned source when first touched, and lands as subcommand + skill.
  A later systematic sweep can fill the remainder without redoing these.
- The rendering oracle is new load-bearing infrastructure. It also closes a
  documented gap that predates W3: `verify`'s visual check has never actually
  compared anything.
