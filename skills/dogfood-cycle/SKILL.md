---
name: dogfood-cycle
description: Rapid-cycle dogfooding — run the real thing against real data, fail, fix, retry until MVP; escalate to a full dev cycle only on a true blocker
mode: skill
triggers: dogfood,dogfooding,try it,rapid cycle,mvp,iterate,does it actually work,smoke test
---

# dogfood-cycle

**Try it → fail → fix it → try again.** Tight loops against the real thing, until
either an **MVP** goal is reached or a genuine blocker forces a bug report and a
full dev cycle.

This is a working mode, not a phase. It is what you do *instead of* declaring
something done because its tests pass.

It has a formal cousin: the **dogfood phase** (ADR-011), run once per release
candidate (`vX.Y.Z-rc.N`). Same activity, opposite discipline — the phase
*logs* every finding as a prioritized GitHub issue and fixes only blockers,
so it measures the product instead of changing it mid-measurement. Issue
text is public: genericise like a commit message.

## Why it earns its place

One pass against a single live site found four defects that a green test suite
had not:

- a `.html` suffix silently becoming part of a route
- an empty route rewritten to an unreachable hash URL, so the homepage vanished
- a Link field whose target record nothing created, failing on any fresh target
- a stale route cache returning 403 to every visitor after pages were replaced

Every one appeared within minutes of actually *using* the artifact. None were
visible from reading the code, and none would ever have been caught by a unit
test, because each lived in the gap between components rather than inside one.

## The loop

1. **Pick a real target.** Real data, real site, real payloads. Synthetic
   fixtures do not surface these bugs — that is the entire point.
2. **Use it the way a user would.** Not "does the function return" — *browse the
   page, click the link, read the output.*
3. **When it fails, diagnose before fixing.** Read the actual mechanism. A guess
   that happens to work leaves the real cause in place. (The 403 above looked
   like a permissions problem and was a cache problem; chasing the symptom would
   have wasted an hour and fixed nothing.)
4. **Fix it, then immediately retry the same step.** Do not batch fixes. One
   change, one retry — otherwise you cannot tell which fix worked.
5. **Reset to a genuinely fresh target before retrying.** Otherwise you verify
   the fix against state your previous attempt already repaired, and the bug
   returns for the next person.
6. **Record every real defect where it will be seen again** — a trap entry, a
   test, an issue. Fixing without recording guarantees rediscovering.
7. **Loop until MVP.** Minimum viable, not complete. Stop when the path works
   end to end.

## When to break the loop

Escalate to a bug report and a full dev cycle **only** for an actual blocker:
something that cannot be fixed from inside the loop — an upstream defect, a
missing capability, a decision that is not yours. Record it, and move on to the
next thing rather than grinding.

Everything else stays in the loop. "This needs a proper refactor" is usually
the loop talking you out of finishing.

## In this project

- **Disposable target:** `buildsmith sandbox up` — a pinned Builder bench. `make
  sandbox-destroy` resets it completely, which is how you get a fresh target.
- **Private layer:** put real client data in `sites/<site>/`. It is gitignored;
  verify with `git check-ignore` before you fetch anything.
- **After each cycle:** `buildsmith test`, and `buildsmith audit` before any thought of
  publishing.
- **Load `builder-safety` first** if the cycle touches components or pages.
