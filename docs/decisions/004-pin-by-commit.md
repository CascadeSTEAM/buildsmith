# ADR-004 — Pin Builder by commit; `1.0.0-dev` identifies nothing

**Status:** accepted · **Date:** 2026-08-03 · **Amends:** ADR-003

## Context

ADR-003 says we target **Builder v1.0.0-dev** and defers the `builder_files/`
standard-page round-trip on the grounds that it is "a later upstream feature and
almost certainly absent there."

Building the sandbox required turning that version string into something
checkoutable. It does not survive the attempt. Read directly from the public
`frappe/builder` history:

| date | commit | what |
|---|---|---|
| 2025-11-17 | `c37cbb7d` | `builder/export_import_standard_page.py` **added** |
| 2025-12-12 | `e6202e23` | develop's `__version__` set from `"1.18.0"` to `"1.0.0-dev"` |
| 2026-06-11 | `e12ca57b` | `Builder Snapshot` doctype **added** |
| *(see below)* | `f0781da9` | **`Builder Variable` renamed to `Builder Token`** (`refactor!`) |

> **The dates in this table were used to reason about ordering, and that was a
> mistake.** `f0781da9` carries a committer date of 2026-07-16 — earlier than the
> commit our target runs — and is nevertheless **not an ancestor of it**. The
> whole rename series shares one rebased timestamp and only reached `develop`
> through a merge on 2026-07-26. Dates order nothing. Use
> `git merge-base --is-ancestor`, or look for the artifact on the host
> (TRAP-016). This ADR argued against trusting a version string and then trusted
> a date, which is the same error wearing a different hat.

Three things follow, and each one contradicts something we had written down.

**1. `1.0.0-dev` is not a release.** It is the placeholder version string carried
by the `develop` branch, set in December 2025. Every develop commit since reports
it — over a thousand as of this writing. It is not a pin; it is barely an
identifier.

**2. The round-trip is not missing.** `export_import_standard_page.py` predates
the version reset. *Any* checkout reporting `1.0.0-dev` already has it. ADR-003's
gating premise is simply false.

**3. A doctype we build on was renamed inside that one version string.**
`Builder Variable` — which the whole token layer, TRAP-004 and TRAP-007 depend on
— became `Builder Token` on 2026-07-05. Two sandboxes both honestly reporting
`1.0.0-dev` can disagree about whether our central doctype exists at all.

## Decision

- **Pin Builder by 40-character commit SHA.** `buildsmith sandbox up` refuses a branch
  or tag outright (override `SANDBOX_ALLOW_LOOSE_PIN=1`, which prints a warning
  and marks results unreproducible).
- **Never treat a reported version string as a version.** For Builder on
  develop, it is not one.
- **ADR-003's deferral of the round-trip is withdrawn** as unsupported. Whether
  M3 is worth doing is now an open question on its merits, not a blocked one.

## The pin, measured twice — because the first measurement was wrong

**Resolved 2026-08-04, on the second attempt. The pin is
`b09a40d98590d9c5eac91a9a9de1795edf364eec`** (develop, 2026-07-20), with the
framework at **frappe 16.27.1** = `f33ac3f00ab818e21b25ddbec93efb653fd9aa1b`
(tag `v16.27.1`, branch `version-16`).

**The first answer, `15cb01e4`, was wrong by 55 commits and 17 days** — and it
was wrong in the most believable way available. It reported *"433 of 435 blobs
byte-identical"* and dismissed the two that were not as a rebuilt `yarn.lock`
and a `__pycache__` entry. Against that commit the real figures are **87 files
differing and 69 host-only**. A match *rate* concealed the answer, which is
precisely the failure recorded in BS-015 about validating a clone by counts:
**the residue was the finding.**

It survived because nothing checked the pin against observable behaviour. It was
eventually caught by accident, while chasing one stray CSS rule: the live site
serves `reset.css?v=6` and the pinned template said `v=5`.

**What the second attempt did differently** — corroborated against things that
are not the tree:

- **`LICENSE` is MIT.** Builder relicensed from AGPL-3.0 on 2026-07-16, so the
  host is provably at or after that date. On its own this disproves `15cb01e4`.
- The served `webpage.html` blob brackets the commit to a one-week window.
- The image was created 2026-07-21T09:43Z, and `develop` HEAD at that instant
  was exactly this commit.
- frappe's tree matches `v16.27.1` **exactly** — 3,317 files, zero differences
  in either direction.

**One honest limit, stated rather than glossed:** tree `3fd08ae9` is shared by
this merge commit and its second parent `fc33913c`. They are
content-indistinguishable. This is the `develop`-line commit, which is what a
`--branch develop` clone lands on.

**Why `git rev-parse` is not available.** The site runs from a pre-built
container image whose build stripped every `.git` directory, so the apps are not
git checkouts: `bench version` prints empty branch parens and Frappe records
`git_branch: UNVERSIONED`. Version provenance has to be recovered from file
contents — and, as this ADR's own history shows, corroborated against behaviour.

**Three wrong answers preceded the right one.** Worth listing, because each was
wrong in a different and instructive way:

| attempt | answer | why it was wrong |
|---|---|---|
| inference | `5fa6ca87` | bounded the right interval from our own observations, then *chose a point inside it* |
| tree match | `15cb01e4` | reported a match **rate** and dismissed the residue; the residue was the answer |
| framework | `develop`, then 16.25.0 | assumed from Builder's CI config, then measured too loosely |

The pattern across all three: each produced a confident, specific, checkable-
looking answer, and nothing checked it. **A measurement nobody corroborates is
an assertion with extra steps.**

## Consequences

- **Confirmed by measurement:** the target has `Builder Variable` (no
  `Builder Token`), `Builder Snapshot`, and `export_import_standard_page.py`
  present on disk. The round-trip is there, contrary to ADR-003.
- **The rename is scheduled, not hypothetical.** `f0781da9` is *not* an ancestor
  of the target's commit, so `Builder Variable` is safe today — but it **is** an
  ancestor of current `develop`, so the rename lands the moment that image is
  rebuilt. `primitives/tokens.py` keeps the doctype name behind one constant so
  that is a one-line change (TRAP-011).
- **Verify the pin against behaviour, not only against the tree.** Nothing did,
  and a wrong pin survived for a day underneath four checks that all report
  results "at the pin". A served asset version disproved it in one line.
- **Both pins are now `confirmed`** in `sandbox/pins.env`, and `init.sh` verifies
  each checkout's `rev-parse HEAD` against them and refuses to continue on a
  mismatch.
- Re-verify `docs/traps.md` against the pin whenever it moves. The ledger is
  commit-specific and nothing about it carries over automatically.
- **Not reproducible in the sandbox:** the target's builder app directory holds
  files written at runtime after the image build — site-authored template
  exports and assets under paths that embed a client-identifying slug. The
  sandbox reproduces upstream Builder, not that site's accumulated state. Any
  question about *its* content has to be answered against the site.
