# ADR-011 — Dogfooding is a release phase, and release candidates say so

**Status:** accepted · 2026-08-07 · owner's direction
**Builds on** the `dogfood-cycle` skill (the working *mode*) and ADR-010's
layered-gates posture.

## Context

The dogfood-cycle skill earns its keep: one pass against a real site found
four defects a green suite had missed. But it is a *working mode* — grab a
tool, use it for real, fix what breaks, retry. Nothing in the process said
**when the whole product must be dogfooded**, so it happened when somebody
felt like it. A release that ships straight from "the suite is green" ships
with exactly the class of defect the skill's own preamble warns about: the
ones that live in the gaps between components.

## Decision

1. **Every release gets a candidate first, and the tag says so:**
   `vX.Y.Z-rc.N`. The plain `vX.Y.Z` tag exists only after its candidate
   survives a dogfood phase. A tag without `-rc` is a promise the phase ran.
2. **The dogfood phase is observe-and-log, not fix-in-loop.** During the
   phase, every finding — bug, feature request, UX wart, doc gap — becomes a
   GitHub issue with a priority label (`P0-blocker` / `P1` / `P2` / `P3`,
   plus `dogfood`). Only a `P0-blocker` interrupts the phase for a fix; all
   else is triaged *after* the phase, so the phase measures the product as it
   is, not as it becomes mid-measurement. (This inverts the dogfood-cycle
   skill's fix-immediately loop on purpose: the mode optimizes for fixing,
   the phase optimizes for *seeing*.)
3. **Leak-watch is part of every phase.** Real usage against a real client
   site is precisely when identifying data tries hardest to escape the
   private layer, so each phase actively watches tool output, artifacts and
   error text for anything the guards would have to catch — and probes the
   guards themselves. Findings get the `leak-watch` label; anything above
   theoretical is a `P0-blocker`.
4. **Issues are public.** Issue text follows the same rule as commits and
   branch names: nothing client-identifying, ever — `a real site`, `<site>`,
   bucketed figures. The guard cannot read what gets typed into a browser;
   this one is on the writer.
5. **Milestones are the gate.** Every finding is filed into the *current*
   milestone with a priority label (P1 "fix before the release tag", P2
   "fix soon", P3 "nice to have"). Triage runs continuously during the
   phase: an issue that should not gate this release is *moved to a later
   milestone*, not closed. **The phase ends when the current milestone has
   zero open issues** — everything in it was either fixed or consciously
   re-homed — and only then is the plain release tag cut.

## What this does not change

The dogfood-cycle *mode* stays exactly as documented for everyday
development: run it for real, fail, fix, retry. The phase is the mode's
formal cousin, run once per release candidate, with the fixing muscle
deliberately disengaged.
