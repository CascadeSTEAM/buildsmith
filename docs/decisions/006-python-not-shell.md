# ADR-006 — One language, two audiences

**Status:** accepted · 2026-08-04

## Context

Buildsmith started as Python emitters driven by a Makefile, with the governance
layer (`guard.sh`, `opskit.sh`, `setup-hooks.sh`, `sandbox/init.sh`,
`poison-test.sh`) written in Bash. That was a reasonable way to bootstrap and a
bad way to ship, for reasons that only became visible once the destination was
clear:

- **It has to run in a container.** The plan is a published image where the
  default entry point is a TUI for designers and content editors. Every Bash
  dependency is another thing that must exist inside that image and behave the
  same as it does on the developer's machine.
- **It has to run for people who are not developers.** `make verify SITE=acme`
  is not an interface; it is a build tool being asked to be one. Make's
  `VAR=value` argument convention also collides with the ordinary `--flag`
  convention the TUI and the container wrapper both need.
- **The shell was drifting from the Python.** The Makefile had grown to 345
  lines with duplicate targets. Logic lived in three places — hook, Makefile,
  CI — and the comments in each said "keep these in sync", which is what a
  design says when it has already lost.
- **Shell hid real bugs.** `for tok in $tokens` word-splits, so a token
  containing a space silently checked something else. The site-isolation and
  branch-name checks ran under `set -e`, so the first failure masked the rest
  and a contributor fixed one violation per commit attempt.

## Decision

**Everything is Python.** One package, `buildsmith`, one entry point,
`buildsmith`, one argument convention, `--flag value`.

Make is deleted. `bin/` is deleted. The remaining shell is two git hooks of two
lines each, because git requires an executable hook file and that is the entire
content of both — every decision they make is in `buildsmith.tools.precommit`.

The one deliberate exception: **OpsKit's `bin/publication-guard.sh` stays a
shell script and is invoked as a subprocess.** It belongs to OpsKit, not here.
Porting it would fork the client-token logic across two repos, which is the
single thing the delegation exists to prevent (ROADMAP section 3).

Bootstrapping is `install.py` — stdlib-only and single-file, because it is what
runs before anything is installed and therefore cannot import the package it is
about to install.

## Consequences

Two install paths, matching the two audiences:

| | Needs on their machine | How they run it |
|---|---|---|
| Designers, content editors | a container runtime | wrapper → container → TUI |
| Developers | Python 3.11+, a container runtime | `python3 install.py --dev`, then `buildsmith` |

Both execute the same code. That is the point of collapsing the languages: the
thing a designer runs in a container and the thing a developer runs from a clone
are not two implementations that have to agree.

Costs accepted:

- Python is slower to start than shell for trivial operations. Irrelevant at the
  granularity anything here runs at.
- Contributors who would have reached for a one-line shell addition now write a
  function and a test. That is the trade we want.

Behaviour that changed rather than being ported literally: the guard now runs
**all** of its checks and reports every violation, where the Bash exited at the
first. One commit attempt now surfaces everything wrong with the tree.
