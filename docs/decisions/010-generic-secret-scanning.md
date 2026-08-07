# ADR-010 — Generic secret scanning is gitleaks, run beside the guard

**Status:** accepted · 2026-08-07
**Builds on** the publication guard (AGENTS.md), BS-004 (push-boundary audit),
BS-025 (history's contents matter).

## Context

The guard family protects against one failure mode: client-identifying facts
reaching a public repo. Its token list, its RFC1918 rule and the audit's
fact patterns (emails, phones, streets, domains) were all written for that.

None of them knows what a *credential* looks like. An AWS access key, a Stripe
token, a `BEGIN PRIVATE KEY` block — all of these pass the guard, the audit
and the pre-push sweep untouched, because nothing in this repo was ever taught
their shapes. Before this ADR, nothing scanned for them at all.

That gap matters more, not less, once the repo is public. A leaked client name
embarrasses; a leaked live credential is an incident with a rotation clock
running.

## Decision

1. **Use gitleaks. Do not write our own rules.** Credential shapes are a
   well-solved problem with hundreds of curated, maintained patterns.
   Reimplementing them here is the same mistake the guard design exists to
   avoid — a forked list that rots quietly. (The guard delegates client
   tokens to the operations project for exactly this reason.)
2. **Beside the guard, never instead of it.** gitleaks knows credentials; it
   has never heard of our clients. The two scanners cover disjoint classes and
   both run at both boundaries: staged diff at pre-commit, full history at
   pre-push and in CI.
3. **Missing scanner = exit 2, and the gate refuses.** This repo's exit-code
   contract: `0` proved, `1` found a problem, `2` could not check. A machine
   without gitleaks must not produce commits that *look* scanned. The
   bootstrap (`install.py --dev`) says so at install time rather than at the
   first surprising refusal.
4. **False positives are allowlisted in `.gitleaks.toml`, in a commit.** The
   review that dismissed a finding should be visible in the diff, not implied
   by a skip variable in someone's shell history. The skip hatch
   (`BUILDSMITH_SKIP_GITLEAKS=1`) exists only for a machine that genuinely
   cannot run the binary.
5. **CI runs the binary directly, pinned by version.** The official
   `gitleaks-action` requires a paid license key for organisation accounts;
   the binary does not. One `curl | tar` of a pinned release keeps CI
   equivalent to the hook without the licensing dependency.

## What this does not cover

GitHub's own **secret scanning + push protection** (free on public repos) is
the forge-side backstop and is enabled as part of going public — see
`docs/going-public.md`. It is a complement, not a replacement: it runs after
the code has already left the machine, which is the boundary our hooks exist
to defend.

## Evidence at adoption

Both scans were clean when this landed: 60 commits of history and the full
working tree, gitleaks 8.30.1, default ruleset. So `.gitleaks.toml` starts
with zero allowlist entries, and any future entry is a reviewed exception
with its reason in the diff.
