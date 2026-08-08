---
name: plow
description: Batch plow-through of the GitHub backlog — clear the open-PR queue (review cycle, merge on green), then dedupe, connect, and prioritize open issues and work each one through the full improvement cycle, one at a time, until the backlog is empty. Use when the operator says "/plow", "plow through", or "clear the backlog".
mode: skill
triggers: plow, /plow, plow through, clear the backlog, backlog sweep, work the queue
---

# plow

One authorized session, the whole backlog. Ported from the operations
project's skill of the same name, re-grounded in this repo's rules — the
per-issue workflow below is self-contained on purpose, so a public clone
can run it without any other project.

1. **Guard + sync.** /plow is repo work only. An issue that needs a live
   system — SSH, a Frappe bench, DNS, any write to a real site — is not plow
   material: skip it, say why, and offer the OpsKit-subagent route
   (AGENTS.md hard rule). `git fetch --all --prune`; pull only if `main` is
   the current branch (worktree sessions base on `origin/main` instead —
   never check out `main`). Announce the toolset (`gh`, `buildsmith test`,
   `/code-review`, `/security-review`, `git worktree`) and get one go/no-go
   for the whole run.

## Phase 1 — clear the PR queue

2. **Collect & prioritize** — `gh pr list --state open`, ordered by the
   triad: **simple over complex, important over less-immediate, impact over
   cosmetic**.
3. **One PR at a time.** Produce a real review. Run `/code-review` against
   it and post the findings to the PR; add `/security-review` when the
   change touches auth, secrets or credentials in any form, the git hooks,
   the publication guard, gitleaks config, CI, or `frappe_client.py`. In a
   harness without those built-in reviewers, post an explicit review pass
   to the PR instead — a PR is never "reviewed" by assertion. Fix findings
   in a worktree of the PR branch (`git worktree add` under
   `.claude/worktrees/`, never by switching a shared checkout), then
   commit, push, and wait for CI on the new head.
4. **Merge on green.** Invoking /plow authorizes the merge: the external
   reviewer is still requested on every PR, but a pending (not-yet-given)
   review does not block. Human-blocked — skip and report, never bypass or
   force-merge: "changes requested", an approval branch protection requires
   but lacks, red CI you cannot fix, or conflicting intent. Repeat until
   the queue is empty.

## Phase 2 — consolidate the issue backlog

5. **Collect** all open issues and read them as one set. **Dedupe &
   connect:** close an unambiguous duplicate as "Duplicate of #n" with a
   cross-reference comment (reversible); an ambiguous overlap gets
   cross-links, both stay open. Link related issues so each survivor is a
   self-contained unit of work.
6. **Prioritize** the survivors: current milestone first — it is the
   release gate (ADR-011: the plain tag is cut only when the current
   milestone has zero open issues) — then `P0-blocker` → `P3`, then the
   triad. Re-triage as you read: an issue that should not gate this release
   is moved to a later milestone, not closed.

## Phase 3 — work the backlog

7. For each issue in priority order — strictly one in flight — run the
   improvement cycle end to end:
   - **Propose.** Root-cause it in the actual code, then post the plan as
     an issue comment (public text — the forge-writes rule below applies).
   - **Critique & research.** Attack the plan before implementing it:
     check `docs/traps.md`, the ADRs, and the code it touches. Resolve the
     findings into the final plan.
   - **Implement.** Issue-linked branch in a fresh worktree, never `main`.
     Load `builder-safety` first if the fix writes any component or page.
     Document as you go; `buildsmith docs` when generated docs go stale.
   - **Completeness.** New behavior gets a test; a safety property gets a
     test that pins it. `buildsmith test` green — and exit 2 means "could
     not check", which is never green.
   - **PR.** `Closes #<n>` in the body; request the external reviewer.
   - **Review & resolve.** The Phase 1 review cycle (step 3), applied to
     your own PR. Address every finding.
   - **Merge** per step 4, then remove the worktree and delete the branch.
8. Re-sync after each merge and pick the next. Stop when the backlog is
   empty or only human-blocked items remain.

## Rules

- Repo hard rules stay in force: linked branch (never `main`), the full
  test gate, the publication guard and gitleaks (never disabled — refused
  work is genericised, not forced through), document-as-you-go. The one
  deliberate, operator-set relaxation is step 4's pending-review merge.
- **Everything /plow writes to the forge is public** — review findings,
  triage and duplicate-close comments, issue plans, PR text. The guards
  only read commits; nothing reads what gets posted, so this one is on the
  writer: nothing client-identifying, ever ("a real site", never whose).
- /plow pre-authorizes exactly two things: merging per step 4 and closing
  unambiguous duplicates. Anything else unrequested is offered, not done.
- During a dogfood *phase* observation run (ADR-011), only a `P0-blocker`
  is fixed mid-run — the run measures the product, and fixing under it
  changes the thing being measured. Between runs, the current milestone's
  backlog is exactly plow material: emptying it is how the phase ends.
- Live-infrastructure issues are excluded per step 1 — skipped and offered
  to an OpsKit subagent, never worked here.

## Failure handling

- A review finding you cannot fix on the spot → comment it on the PR,
  skip, continue.
- Two failed attempts at the same fix → stop that item, record progress on
  the issue, move on. Grinding a third time is how loops eat sessions.
- Always end with a report: PRs merged, duplicates closed, issues
  completed, items skipped and why.
