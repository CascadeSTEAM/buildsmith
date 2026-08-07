---
name: builder-optimize
description: W3 Optimize (ADR-009) — builderize a site already in the sandbox; baseline first, oracle after every transform
mode: skill
triggers: optimize,builderize,tokenize,baseline,oracle,pop,improve site,rendering unchanged
---

# builder-optimize

**First load `builder-safety`** — every transform here ends in Builder records,
and the transforms that make optimization worth doing (collapse, componentize)
are precisely the ones the trap ledger is about.

W3 takes Builder records in the sandbox and does not care how they got there:
`adopt → optimize` (a customer site) or `import → optimize` (a scraped one).
Two phases with opposite proofs — ADR-009 is the contract, read it before
deviating:

- **Phase A** must prove it changed *nothing visible*. The proof is the
  rendering oracle, not a claim.
- **Phase B** must change what's visible, *deliberately* — one reviewable
  proposal at a time, human-adjudicated, expressed only in Phase A's
  vocabulary (tokens and components, never new literals).

## The loop

0. `buildsmith optimize status --site <s>` — where am I? Reads artifacts only
   (baseline, gate ledger, proposal files); pending unproven transforms are
   its headline. `--json` for the machine view.
1. `buildsmith optimize baseline --site <s>` — before the first transform and
   after every accepted one. Captures crawl, features.json, deterministic
   screenshots, a record checkpoint, and the script-dependency scan. It
   refuses while the gate ledger holds an applied-but-unproven transform;
   `--force` waives, and the waiver is recorded.
2. Apply one transform. Every `--apply` runs the oracle itself and records
   the verdict in the gate ledger.
3. `buildsmith optimize oracle --site <s>` — exit 0 is the only pass. Exit 2
   ("could not check") is never a pass; fix the reason and re-run. Only a
   default-parameter run settles the gate ledger — a loosened threshold
   proves nothing and is recorded as nothing.
4. On FAIL, read the diff artifacts under `sites/<s>/opt/oracle/` — the
   changed pixels are painted red. Diagnose before fixing (dogfood-cycle).
5. Journal entries are appended automatically; add notes for anything a
   future session would need.

## Invariants the tooling holds you to

- The oracle's threshold is headroom for rendering drift, not for change —
  an unchanged site measures 0.0000%.
- A route that stops being captured is a structural failure, not a skip.
- `features.json` is extracted, never hand-written.
- Unpublished routes are recorded as skipped, never silently dropped.
