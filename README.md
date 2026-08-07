# buildsmith

Tools for building and caring for [Frappe Builder](https://frappe.io/builder)
websites — design tokens, reusable components, page templates, data-driven
pages, and faithful full-site replication.

Buildsmith exists because a website you can't maintain is a website you'll
eventually abandon. Every build here ends with a reusable design system, not
just a pile of pages. It's free software, built in the open, and meant to be
learned from as much as used.

> **Status: working, pre-publication.** The primitives, all three workflows
> (replicate · theme & maintain · optimize), the pinned sandbox, and a
> 400-plus-test suite are built and in daily use on real work. What remains
> before the repo goes public is tracked in `docs/ISSUES.md`; the plan is in
> `docs/ROADMAP.md`.

## The three workflows

- **Replicate** — take an existing site and produce a faithful, complete
  Builder copy, with every route preserved. A careful copy, not a redesign.
- **Theme & maintain** — grow a site's design system over time: a token
  manifest becomes components, components become a page template, templates
  drive data-driven pages.
- **Optimize** — take a site already in the sandbox and *builderize* it:
  baseline → tokenize → fonts → collapse → componentize. Every transform is
  checked by a rendering oracle (a before/after screenshot referee) and
  recorded in a ledger, so nothing changes how the site looks without proof.

Every build emits a template — design tokens, reusable components, *and* a
`Builder Page` with `is_template=1`. Skipping that step is what makes a site
expensive to maintain later, so no build here skips it.

## Tools emit files. They never touch a live site.

This is the rule that makes Buildsmith safe to point at real work.

**No tool here can reach a site outside `LOCAL_ONLY`** — a two-name
allow-list of disposable local dev sites, with no override flag, on purpose.
Publishing means handing the emitted payload to an operator (or an agent)
with the access to apply it. This is a structural property of the codebase,
not a convention — keep it that way.

Exactly one module, `buildsmith/tools/frappe_client.py`, can speak HTTP to a
Frappe instance at all. Three gates run before any write: the site name, the
shape of the host, and whether a worker is actually draining the queue. The
tests pin the constructor's signature, so adding an override *fails* the
suite instead of passing it.

Credentials are **read, never resolved**. A token comes in from the
environment, and Buildsmith has no client for any secret store. Why the
promise is phrased around the target instead of the transport:
`docs/decisions/007-the-guarantee-is-the-target.md`.

## Getting started

```sh
python3 install.py --dev     # virtualenv, editable install, browser, git hooks
source .venv/bin/activate
buildsmith test              # prove the install, including the safety gates
```

You'll also want [gitleaks](https://github.com/gitleaks/gitleaks/releases) —
a one-binary secret scanner the commit gate depends on. `install.py --dev`
checks for it and tells you if it's missing.

Designers and content editors don't need any of that. Buildsmith runs out of
a container for design and content work, and `python3 install.py` checks for
the one thing that requires (Docker or Podman). The image and its wrapper are
not built yet; see `docs/decisions/006-python-not-shell.md`.

Installing the git hooks is part of `--dev`, and it is **per-clone by
necessity**. Git's `core.hooksPath` is local config, so a fresh clone has
every gate switched off until something turns it on. `buildsmith hooks
--check` reports; `buildsmith hooks` installs.

## The safety gates

This repo is meant to be published, and it gets used against real client
sites — so what leaves this machine is guarded in layers:

- **Publication guard** (pre-commit): no client names, no private network
  addresses, no private site files — in tracked files, paths, branch names,
  or commit messages. Token scanning is delegated to a companion operations
  repo: one token list, one place to add a client.
- **Secret scan** (pre-commit + pre-push): [gitleaks](https://github.com/gitleaks/gitleaks)
  checks for credentials — API keys, tokens, private keys — in the staged
  diff at commit time and across **all of history** before a push. See
  `docs/decisions/010-generic-secret-scanning.md`.
- **Publication audit** (pre-push): the whole tree, the whole history, every
  ref, re-checked for identifying facts before anything leaves the machine.
- **CI** re-runs everything it can on GitHub, because hooks are local config
  and CI is the copy of the gate that can't be uninstalled. The one
  exception: the client-token audit needs the token list, which is itself a
  secret — so it stays fail-closed only where the tokens live, in the
  maintainer's pre-push hook.

Every gate **fails closed**: if a check cannot run, the commit or push is
refused rather than waved through. An unscanned change must never look like a
clean one. And never disable a gate to get a file committed — make the file
publishable instead. **Facts leak too**: a sentence describing what runs
where can identify a client without naming one.

## Layout

```
buildsmith/
  cli.py            the one entry point; every subcommand routes from here
  primitives/       shared building blocks: tokens, blocks, components, templates
  workflows/        replicate/, theme/ and optimize/
  tools/            guard, secretscan, hooks, sandbox, simulate, validate,
                    docgen, journal, clone-diff, visual-check, drift,
                    publish-verify, handoff
install.py        stdlib-only bootstrap; runs before anything is installed
sandbox/          disposable Builder bench, pinned to a known commit
sites/example/    committed fictional fixture
sites/<site>/     gitignored private layer — brand, content, state, journal
docs/             roadmap, traps, decisions, going-public runbook
```

Everything is Python — no Make, no shell beyond three few-line git hook
shims. Why: `docs/decisions/006-python-not-shell.md`.

## Documentation

`AGENTS.md` is the working guide for humans and AI agents alike.
`docs/decisions/` records why things are the way they are. Docs here aim for
plain language on purpose — see the writing notes in
[CONTRIBUTING.md](CONTRIBUTING.md).

## Community

New here? Start with [CONTRIBUTING.md](CONTRIBUTING.md). Everyone
participating is expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md). Found a security problem? Please read
[SECURITY.md](SECURITY.md) — privately.

## Licence

MIT — see `LICENSE`. Take it, learn from it, build with it.
