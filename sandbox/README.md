# sandbox

A disposable Frappe bench running Builder at a **pinned commit**, for trying
things that must not be tried on a live site.

```sh
buildsmith sandbox up        # build it (slow the first time)
buildsmith check traps       # prove it is faithful
buildsmith sandbox status    # what exists right now (and the login)
buildsmith sandbox destroy
```

The site is `sandbox.localhost`, admin password `admin`, bound to
`127.0.0.1:8000` only. Nothing in it is precious — `destroy` and rebuild is the
expected response to almost any problem.

## Why this is in-repo at all

Every other piece of infrastructure in this project is somebody else's job
(ADR-002). This one is not, because **a publishable project has to be runnable by
someone who does not have the operations project.** The line: local disposable
containers live here, anything touching a server does not.

## The pin is a commit, and that is not pedantry

`sandbox/pins.env` holds a 40-character SHA and `init.sh` refuses anything else.

Builder's `develop` branch has reported `__version__ = "1.0.0-dev"` since
2025-12-12. Within that single unchanged string it has added the `Builder
Snapshot` doctype and **renamed `Builder Variable` to `Builder Token`** — a
doctype most of our tooling is built on. Two sandboxes can therefore both report
`1.0.0-dev` and disagree about whether our central doctype exists.

A sandbox on a different Builder than the target is not a test bed. It is a
second opinion from a stranger.

Full evidence and the derivation of the current pin: **ADR-004**.

Both pins are marked `confirmed` in `pins.env`, which also records the full
derivation — including the two wrong answers that preceded each.

## `buildsmith check traps` is the point

A sandbox that merely boots proves nothing. The exit criterion is that it
**reproduces a known trap**, so we know it fails the way production fails.

It reproduces the ledger's verified traps against the pin — 15 checks today,
covering TRAP-001 and the TRAP-003 rules. The founding example is TRAP-003: a
repeater block needs `isRepeaterBlock` **and** `children` **and** `dataKey`,
and dropping any one of them degrades it to an ordinary block with no error.
The observable is that a working repeater emits a Jinja `{% for %}` loop into
the rendered HTML and a broken one silently does not.

If `check traps` fails, the sandbox disagrees with the trap ledger. Either the
pin is wrong or `docs/traps.md` is stale — and until that is resolved, nothing
else the sandbox reports is worth reading.

## Both pins are confirmed against the target

Builder `b09a40d9` and frappe `f33ac3f0` (16.27.1, branch `version-16`) —
**`sandbox/pins.env` is the authority**; if this paragraph and that file ever
disagree, the file wins. Earlier answers (`15cb01e4`, then `9a8daf34`/16.25.0)
were each confirmed-sounding and wrong; pins.env records how the current ones
were actually established. `init.sh` verifies each checkout's `rev-parse HEAD`
against `pins.env` and refuses to continue on a mismatch.

Worth knowing how that was established, because the obvious method does not
work here: **the target's apps are not git checkouts.** It runs from a container
image whose build stripped every `.git` directory, so `bench version` prints
empty branch parens and Frappe records `git_branch: UNVERSIONED`. The commits
were recovered by hashing the on-disk tree and matching upstream blobs — 452 of
454 builder blobs byte-identical at the confirmed pin (the first, wrong answer
also had an impressive match rate; `pins.env` tells that story). Any future version question about that
deployment needs the same approach; `git` will confidently tell you nothing.

An earlier build of this sandbox ran frappe **17.x.x-develop** against a July
2026 Builder, a pairing that exists nowhere, because the framework was assumed
from Builder's CI configuration rather than measured. That is fixed.

## What the sandbox still does not reproduce

Upstream Builder, not *that site*. The target's app directory holds files written
at runtime after its image was built — site-authored template exports and assets
under client-specific paths. Questions about the site's own accumulated content
have to be answered against the site.

## Known limitation — no scheduler, no workers

`init.sh` builds the bench but does not run `bench start`, so nothing is draining
the queue. Rendering does not care. **Publishing does**: `queue_action` enqueues
work that will never run, which is TRAP-009 — documents lock and stay locked.

Fine for the trap checks as they stand. Anything that publishes a page or
component from inside the sandbox needs the workers up first.
