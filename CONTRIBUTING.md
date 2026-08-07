# Contributing to Buildsmith

Hey — glad you're here. Buildsmith is a community-minded tool for building
and caring for [Frappe Builder](https://frappe.io/builder) websites, and it
gets better every time someone new kicks the tires. Whether you're a seasoned
FOSS contributor, a student learning the ropes, or a maker who found a bug at
2 AM: you're welcome, and your contribution counts.

## The short version

1. Be kind. We follow the [Code of Conduct](CODE_OF_CONDUCT.md).
2. Run `python3 install.py --dev` before anything else. It sets up the safety
   rails, not just the tooling.
3. Never commit secrets, client names, or private network details. The hooks
   will catch most of it, but the hooks are the backstop — you are the policy.
4. Open an issue before a big change. Small fixes can go straight to a pull
   request.
5. Write docs the way this file is written: plain words, short sentences,
   jargon explained the first time it shows up.

## Getting set up

```sh
git clone <this repo>
cd buildsmith
python3 install.py --dev     # virtualenv, editable install, browser, git hooks
source .venv/bin/activate
buildsmith test              # prove the install works
```

One extra tool is required: [gitleaks](https://github.com/gitleaks/gitleaks/releases),
a secret scanner. It's a single binary with no dependencies. Commits are
refused until it's installed — that's on purpose. A commit that *looks*
scanned but wasn't is worse than a loud error.

## The safety rails, and why they're strict

This repo is public, but it gets used against real client websites. So every
commit and every push passes through gates:

- **The publication guard** checks that no client name, private address, or
  private site file sneaks into a commit, a path, a branch name, or a commit
  message.
- **The secret scan** (gitleaks) checks for credentials — API keys, tokens,
  private keys. Yours *and* anyone else's.
- **The pre-push audit** re-checks the whole tree and the whole history,
  because a leak that never leaves your machine is fixable and one that
  reaches a public remote is not.
- **The test suite** runs before every push. Green by habit is a streak;
  green by hook is a property.

If a gate refuses your work, read what it printed — every refusal explains
itself and names the fix. Please don't reach for `--no-verify`; it skips
*every* gate, including the one that would catch the next problem. If you hit
a false positive, that's a bug worth reporting on its own.

**Exit codes mean something here:** `0` proved, `1` found a problem, `2`
could not check. That third one matters — "I couldn't verify this" must never
be dressed up as "this is fine."

## What we're building (so your change fits)

Two rules shape almost every design decision:

- **Tools emit files. They never touch a live site.** No tool in this repo
  can reach a website outside a two-name local allow-list, and there is no
  override flag on purpose. If your change needs to talk to a real site,
  the answer is to emit a payload a human (or an operations tool) applies.
- **Every site build emits a template.** Design tokens, reusable components,
  and a template page. Skipping that step is what makes a site expensive to
  maintain later — and helping people maintain what they build is the point.

`AGENTS.md` is the full working guide, and `docs/decisions/` records *why*
things are the way they are. If a decision seems odd, there's usually an ADR
(architecture decision record — a short "here's what we chose and why" note)
with the story.

## Writing docs

Documentation here aims for an **eighth-grade reading level**. That's not
dumbing down — it's opening up. Plain writing welcomes students, hobbyists,
folks reading in their second language, and tired professionals equally.
Concretely:

- Short sentences. One idea each.
- Common words where they work. Technical terms where they're needed —
  defined the first time.
- Say *why*, not just *what*. The why is what survives.

## Pull requests

- Branch off `main`, keep the change focused, and let the hooks run.
- New behavior gets a test. A safety property gets a test that *pins* it —
  plenty of examples in `tests/`.
- If your change makes the generated docs stale, the commit will tell you;
  `buildsmith docs` regenerates them.

Thanks for building with us. Share what you learn — that's the whole game.

— The NetYeti
