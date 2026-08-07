# ADR-002 — Actions belong to the operations project; artifacts belong here

**Status:** accepted · **Date:** 2026-08-03

## Context

Buildsmith sits next to a private operations project that already owns live-system
access: credentials, SSH, deploys, DNS, ticketing, and the publication guard.
Two earlier drafts of the plan each got the split wrong in a different direction.

The first rebuilt about ten things the operations project already ships — its own
guard, its own token list, its own credential flags — quietly re-introducing
secret handling into a repo meant to be published.

The second overcorrected: it moved Buildsmith's *private website data* into the
operations project's per-client environment directories. The argument was that a
second private area forces a second client-token list. That argument is invalid —
the token list and the data location are independent, since the hook calls the
operations project's guard either way. One list either way.

## Decision

> **The operations project owns *actions* against live systems** — access,
> secrets, deploys, DNS, ticketing, and guard-as-a-service.
> **Buildsmith owns *design artifacts*, including its private ones.**

**Delegate by capability, never by sensitivity.** Data lives with its owner.

## Consequences

- Buildsmith's private site layer (`sites/<site>/`, gitignored) stays in
  Buildsmith. Website copy and infrastructure inventory have different
  lifecycles, different reviewers and different tooling; filing them together
  organises by sensitivity instead of ownership.
- Buildsmith never reimplements the guard, the token list, secret resolution,
  provisioning, DNS, or ticketing. It calls them.
- Buildsmith must remain usable **standalone**, by someone with no operations
  project at all — a demo site with no client and no infrastructure. A public
  project cannot document its private-layer story as "put it inside this other
  private project."
- Filing something elsewhere *because it is private* is the specific mistake this
  rule exists to prevent.

### Deliberate exception: the local sandbox stays in-repo

Strict infrastructure-as-code would route even local container provisioning to a
playbook. `sandbox/docker-compose.yml` stays here anyway, because a publishable
project must be runnable by someone who does not have the operations project.
The line: *local disposable containers → in-repo compose; anything touching a
server → the operations project.*
