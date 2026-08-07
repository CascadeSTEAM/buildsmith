# Going public — the forge setup runbook

This is the checklist for the day Buildsmith gets its remote. It also covers
the settings that only exist *on* the forge (GitHub) once it does. Everything
here is run by the owner, once. It is not a substitute for the pre-flight
gate in `docs/ISSUES.md` §"Before creating the remote" — that list must be
green first, and this file assumes it is.

Why put protection on GitHub at all, when the hooks already gate every commit
and push? Because hooks are per-clone local config. Anyone — including us on
a fresh machine — can clone this repo with every gate switched off, until
`install.py --dev` turns them on. The forge settings are the copy of the
rules that cannot be uninstalled.

## 1 · Create the repo private, push, confirm CI

The roadmap says the repo starts **private** and flips public only after the
pre-flight gate. Keep that order — you cannot un-publish a git history.

```sh
# from the repo root, after the ISSUES.md gate is green
git push -u origin main
```

Then add the one CI secret. The client-token list is itself a secret, so CI
receives it through the forge, never through a file:

```sh
gh secret set CLIENT_TOKENS   # paste the list; whitespace/comma separated
```

Watch the first `ci` run. Both jobs (`test`, `secretscan`) must pass before
anything else in this file matters.

## 2 · Protect `main`

Two tiers. Start with the first; move to the second when someone besides the
owner has write access.

**Tier 1 — solo maintainer (now).** Nobody can delete `main` or rewrite its
history. The owner can still push it directly, because the local pre-push
hook is the gate:

```sh
gh api -X POST "repos/{owner}/{repo}/rulesets" --input - <<'EOF'
{
  "name": "protect-main",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] } },
  "rules": [
    { "type": "deletion" },
    { "type": "non_fast_forward" }
  ]
}
EOF
```

**Tier 2 — collaborators (later).** Add pull requests and green CI as
requirements. This *will* block direct pushes to `main`, including the
owner's, so adopt it when the PR flow is the flow:

```sh
gh api -X POST "repos/{owner}/{repo}/rulesets" --input - <<'EOF'
{
  "name": "require-review-and-ci",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] } },
  "rules": [
    { "type": "pull_request",
      "parameters": {
        "required_approving_review_count": 1,
        "dismiss_stale_reviews_on_push": true,
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_review_thread_resolution": false
      } },
    { "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": true,
        "required_status_checks": [
          { "context": "test" },
          { "context": "secretscan" }
        ]
      } }
  ]
}
EOF
```

## 3 · The community files are already in the tree

`CONTRIBUTING.md`, `CODE_OF_CONDUCT.md` and `SECURITY.md` ship with the repo.
GitHub's community-profile checks light up on their own. Two things still
need a click in the web UI, because there is no clean API for them:

- **Settings → General**: disable the wiki (docs live in `docs/`, one home).
- **Settings → Advanced Security**: confirm *private vulnerability reporting*
  is enabled, so `SECURITY.md`'s reporting path actually exists.

Dependency alerts and automatic security fixes work on private repos, so turn
them on now (this repo's dependency surface is small, which is exactly why
the alerts are cheap to keep on):

```sh
gh api -X PUT "repos/{owner}/{repo}/vulnerability-alerts"
gh api -X PUT "repos/{owner}/{repo}/automated-security-fixes"
```

## 4 · Flip public

Re-run the last pre-flight items **immediately before** flipping. The
cheapest regressions happen at the last minute:

```sh
git ls-files sites/        # must list only sites/example/
buildsmith audit           # must be clean, with a non-empty token list
buildsmith secretscan --history
```

Then: **Settings → General → Change visibility → Public.** Tag the moment —
`git tag -a v0.1.0 -m "first public cut"` — so "what was public from day one"
is a ref, not a memory.

## 5 · Turn on the forge's own scanners (public only)

GitHub's secret scanning is **free only on public repos** — on a private one
this call is refused unless you pay for Advanced Security, which is why this
step comes *after* the flip. It complements gitleaks (ADR-010): it runs
server-side, with patterns registered directly by the credential issuers.
**Push protection** goes one further: it refuses a push containing a match
before the push lands.

```sh
gh api -X PATCH "repos/{owner}/{repo}" --input - <<'EOF'
{
  "security_and_analysis": {
    "secret_scanning": { "status": "enabled" },
    "secret_scanning_push_protection": { "status": "enabled" }
  }
}
EOF
```

Run this the same hour the repo flips — the window where "public" and
"unwatched" overlap is the one to keep short.
