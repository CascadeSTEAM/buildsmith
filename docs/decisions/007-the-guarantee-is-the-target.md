# ADR-007 — The guarantee is the target, not the transport

**Status:** accepted · 2026-08-04 · owner's decision
**Supersedes** the "no tool here opens a socket" wording in README.md and AGENTS.md.

## Context

Buildsmith documented a structural property in emphatic terms:

> No tool here opens a socket to a Frappe instance and none takes credentials.
> This is a structural property of the codebase, not a convention — keep it that
> way.

It was true, and it was load-bearing: it meant no bug, no bad argument and no
confused agent could write to a client's live site, because nothing here could
reach one at all.

It also could not survive the container architecture (ADR-006, ROADMAP M4). A
Buildsmith container talking to a sibling Frappe container has three options,
and only one of them is any good:

| | |
|---|---|
| Mount the Docker socket | root-equivalent on the host, for an image we intend designers to run |
| A separate applier service | keeps the sentence literally true; one more service to build and keep in sync |
| Speak HTTP, gate the target | the sentence stops being true; the guarantee has to move |

The observation that settled it: **the sentence was never what protected us.**
`load_dev` already wrote to a Frappe instance — it just did so by shelling into
a container rather than over a socket. What actually stopped it touching a live
site was `LOCAL_ONLY`, a two-hostname allow-list with no override flag. The
"no socket" property was a description of the transport that happened to imply
the guarantee. Transports change. The allow-list is the guarantee.

## Decision

The invariant is restated in terms of the target:

> **No tool here can reach a site outside `LOCAL_ONLY`.** The allow-list has no
> override flag, on purpose. Credentials are read from the environment and never
> resolved — that stays the operations project's job.

One module, `buildsmith/tools/frappe_client.py`, is the only thing in the
package that speaks HTTP to a Frappe instance. Three gates run before any write:

1. **Site name** in `LOCAL_ONLY`. No flag, no environment variable, no argument
   relaxes it. `tests/test_frappe_client.py` asserts the constructor's signature
   so that adding a `force=` argument fails the suite rather than passing it.
2. **Host shape** — loopback, a `.localhost` name, or a dotless label (a
   container service name on a private network). A public FQDN or routable IP is
   refused *before authenticating*. This closes the one real hole in gate 1:
   Frappe resolves its site from the `Host` header, so a mis-set base URL plus a
   valid token could otherwise satisfy the site allow-list while the bytes land
   somewhere real.
3. **A live worker** must be draining the queue (TRAP-009, TRAP-017).

Writes require token auth (`Authorization: token <key>:<secret>`) rather than a
session. That is not a preference — Frappe applies CSRF protection to
cookie-authenticated POSTs, so a session login cannot write without scraping a
CSRF token out of the desk boot payload. A scoped, revocable API token is both
simpler and the right credential shape.

## Consequences

**What we keep.** Nothing in the package can write to a live site. The
guarantee is now stated where it is enforced instead of being an emergent
property of how the code happened to be written, and it is covered by tests that
fail loudly if it is widened.

**What we give up.** The emitter code *can* speak HTTP. Someone determined to
publish to live from this repo now has to defeat two allow-lists rather than
write a network client from scratch. That is a real reduction in the strength of
the boundary and it was made deliberately, with the owner's decision on record.

**Residual risk, stated rather than discovered.** A private-network Frappe
reachable at a dotless hostname, serving a site literally named
`sandbox.localhost`, with a valid token in the environment, would be written to.
That requires deliberate misconfiguration on three axes at once. It is not
defended against, and the alternative that would defend against it — the
separate applier service — remains available if the boundary ever needs to be
restored.

**What did not change.** Applying anything to a live site is still an action and
still belongs to the operations project (ADR-002). Secret *resolution* is still
theirs too: Buildsmith reads `BUILDSMITH_FRAPPE_TOKEN` and has no client for any
secret store, which `tests/test_frappe_client.py` enforces by inspecting the
module's imports.
