# Open issues

A stand-in issue tracker until this repo has a remote. **Every `BS-` item below
must be resolved or consciously accepted before `origin` is created** — that is
the whole point of the list.

Raised 2026-08-04 by a dogfooding run against a real client site, plus the
publication audit that followed it. Findings are evidence-based: each was
reproduced, not theorised.

Status: `open` · `done` · `accepted` (a known limitation we are choosing to live
with, with the reason recorded)

---

## Blocking — must be closed before the repo gets a remote

### BS-001 · The client-token list does not cover every client · **done, 2026-08-06**

**Evidence.** The guard's token list is assembled from the operations project's
`.client-tokens` plus its `environments/*` directory names. A live client site
replicated during dogfooding had **no matching token at all** — not in the file,
not as an environment directory. Their business name, their domain, and their
ERP subdomain all passed the guard untouched.

**Why it matters.** The guard's entire client-name defence is a fixed list. A
client missing from it is a client the guard has never heard of, and nothing
announces the omission — the commit simply passes.

**Not just hypothetical.** The same client's name has already reached a
public origin in a neighbouring repo, in more than one form. An operations
session note from 2026-08-03 had already flagged one of them as a known gap;
scrubbing that repo is tracked on the operations side.

**Fix (done).** The missing tokens were added to the operations project's
`.client-tokens` (theirs, gitignored — the change belonged there, not here)
and `buildsmith audit` re-run clean; BS-025's history work ran against the
completed roster.

**Open question for the owner:** whether to also cover the *organisation's own*
name. The token list holds a short abbreviation for it, and word-boundary
matching means that abbreviation does not match the full name — so the full form
passes today, including inside container image paths. Adding the long form makes
the guard stricter everywhere, including in repos where that name is legitimately
expected. A judgement call, not a defect.

_(This paragraph was itself rejected by the guard on first writing, because it
quoted the abbreviation literally. Fair catch, and a neat illustration of why
this file must be written in the general form.)_

---

### BS-002 · Nothing checks branch names · **done**

**Evidence.** A branch named after a client passes the guard, because the guard
only inspects the staged diff. `AGENTS.md` admitted this in as many words:
"keep client names out of branch names as well (nothing checks those — that one
is on you)."

**Why it matters.** Branch names are published the moment they are pushed, and
they persist in the remote's ref list and in PR titles long after the branch is
deleted locally. This has already happened in a neighbouring repo.

**Fixed.** `buildsmith guard` now checks the branch name at commit time, and
`buildsmith audit` sweeps refs. The poison test covers it.

One subtlety worth keeping: the first implementation used
`git rev-parse --abbrev-ref HEAD`, which **fails on an unborn branch** and so
silently skipped the check on the very first commit — precisely when a fresh
client-named branch is most likely to exist. It uses `git symbolic-ref` now.

---

### BS-003 · Facts identify without tokens · **accepted, mitigated**

**Evidence.** All of the following passed the guard, using a real client's real
data: their public domain, their ERP subdomain, their public WAN IP, their
street/neighbourhood, their phone number, and their container image name. Only
RFC1918 addresses and listed tokens were caught.

**Why it matters.** `AGENTS.md` already says "facts leak too", but nothing
enforced it. A phone number and a neighbourhood name identify a business as
precisely as its name does.

**Mitigation shipped.** `buildsmith audit` detects emails, phone numbers, street
addresses, routable IPs, real domains and URLs, with documentation-reserved
values allowlisted.

**Why accepted rather than done:** a pattern scan cannot catch a *description*.
"A surf shop on a fishing pier whose site we rebuilt last spring" (invented,
unlike any client) names nobody and
identifies one business. The final defence before going public is a human
read-through, and this issue stays open in spirit as a reminder of that.

---

### BS-004 · The guard only sees the staged diff · **fixed, 2026-08-04**

**Evidence.** With a leak already committed in the tree, staging an unrelated
file and committing passes cleanly. The guard never looks at what is already
there, nor at history.

**Why it matters.** One leak that slips through once is permanent and invisible
thereafter. Before flipping public, history is what gets read.

**Partly fixed.** `buildsmith audit --scope all` covers tree, history, commit
messages and refs, and `buildsmith audit` runs it. Currently **clean**.

Now wired: `.githooks/pre-push` runs the unit suite (sub-second; a pushed tree
must be green by property, not by streak) and then `buildsmith audit --scope
all`, refusing the push on any red or any finding. Deliberately not in
pre-commit — scanning full history on every commit is slow, and a slow
pre-commit hook is one somebody disables, at which point the fast checks stop
running too. Pre-commit instead lints the staged Python with ruff, failing
closed when ruff is missing.

Push is the right boundary. A leak that never left the machine can be fixed by
rewriting history; one that reached a remote cannot — it is in someone's clone
and in the forge's object store, and may outlive deletion.

`BUILDSMITH_SKIP_AUDIT=1 git push` is the escape hatch, and the refusal message
names it on purpose: an escape hatch nobody can find gets replaced by
`--no-verify`, which skips *every* hook including the ones that would have
caught the next problem.

---

### BS-005 · A live credential sits in cleartext on disk · **open — owner**

**Evidence.** The operations project's `.claude/settings.local.json` contains a
plaintext infrastructure API bearer token and a plaintext vault session key,
baked into permission allow-list entries. Reported by the lookup subagent as an
aside, unprompted.

**Why it matters.** The file is gitignored, so this is not a publication leak —
it is a credential-hygiene problem. Those are live secrets in a world-readable
file on a workstation.

**Fix.** Rotate both, and move them behind the vault rather than inlining them
into settings. **Owner's call, in the operations project — not this repo's to
make.**

---

## Non-blocking

### BS-006 · `.html` suffixes were becoming routes · **done**

Found dogfooding: a crawl saved to disk and read back turned `/menu` into
`/menu.html`, so every replicated page would have published at a URL the source
site never had. Routes preserved is W1's success floor and this broke it
silently. Fixed in `route_for()`; four regression tests added.

### BS-007 · The audit's first draft was unreadable · **done**

The domain detector's obvious pattern — label dot label — matched `block.get`,
`json.loads` and `parent.parent`, producing **1620 findings** on this repo's own
source. A detector nobody reads protects nothing. Now anchored on unambiguous
public suffixes plus an explicit URL rule, with TLDs that double as Python
attribute names (`site`, `name`, `page`, `app`, `io`) deliberately excluded.

### BS-008 · Tests used `http://x/` as a placeholder · **done**

Caught by the audit. Replaced with `http://example.test/`, which is
documentation-reserved and cannot ever be somebody's real host.

### BS-009 · Design tokens do not survive the round-trip · **accepted**

An upstream Builder bug — `export_variables()` cannot match a uuid-named
variable, so tokens are silently omitted from `builder_files/` exports. Recorded
as TRAP-013 and ADR-005; `sandbox/roundtrip-check.py` asserts the bug on purpose
so we learn when upstream fixes it. Nothing for us to fix, but any delivery on
that layer must apply tokens separately.

### BS-011 · Empty routes made the homepage unreachable · **done**

Browsing the replica: `/` served the Frappe desk login. The homepage existed and
was published, at `pages/page-0522e317` — because `set_default_values()` rewrites
an empty route to `pages/<name>`, and the name is an unchooseable hash
(TRAP-012). The site's front door is `Website Settings.home_page`, which nothing
set. Recorded as **TRAP-014**. `page()` now refuses an empty route and W1 gives
the home page the route `home`.

### BS-012 · `project_folder` had no target record · **done**

Applying our payloads to a fresh site failed with
`LinkValidationError: Could not find Project Folder: buildsmith`. It is a Link
field and nothing creates the folder. Payload validation cannot catch this — it
is a fact about the *target*, not the payload. Added
`primitives.template.prerequisites()`, surfaced in `buildsmith golive`, in the handoff
brief, and in both build summaries.

### BS-013 · A replaced page 403s until the cache is cleared · **done**

After re-applying pages to a fresh target, **every route returned 403 Not
Permitted** — including ones that had worked minutes before. `published=1`,
correct route, `authenticated_access=0`; everything looked right.
`find_page_with_path()` is redis-cached for an hour, so a replaced page leaves
its route pointing at a deleted docname, and the failure surfaces as a
permission error rather than a 404 — which sends you hunting through permissions
instead of the cache. Recorded as **TRAP-015**; clearing the website cache is now
a post-apply step in the go-live plan and the handoff brief.

### BS-014 · The audit flagged its own explanatory comment · **done**

`buildsmith audit` reported a routable IP in `buildsmith audit:178` — the comment
`# a version string like 16.25.0.1, not an address`. Third time a detector has
tripped on this project describing its own rules (after the 1620-finding domain
pattern and `ISSUES.md` quoting a token while explaining tokens). The
`_SELF_REFERENTIAL` exclusion exists for exactly this and now covers it.

Worth naming as a pattern: **a scanner that documents what it looks for will
find itself.** Verified the fix does not blunt detection — a throwaway file with
a real public IP and a real phone number is still caught.

### BS-015 · Validation compared counts, not content · **done — the root cause**

Three features went missing from a clone I had reported as matching: a hover
menu, a gallery lightbox, and the hero background image. All three were found
by the owner **looking at the page**. None were caught by any check I had.

The converter was not really the problem. **The validation was.** I compared
declaration counts, image counts and byte sizes, saw 375 against 376, and called
it a match — but 375 of 376 declarations can be 375 *different* declarations. A
count cannot distinguish "nearly identical" from "entirely different", and I used
one as proof.

**Fixed with three tools, in increasing order of what they can prove:**

- `bin/clone-diff.py` — set differences on selectors, per-selector declarations,
  assets, text runs, links and scripts. Counts stay as a cheap tripwire; this is
  the verdict.
- `sites/<site>/features.json` — a feature inventory **extracted from the source,
  never hand-written**. A hand-written checklist contains only the features
  somebody remembered, and the problem is always the ones nobody noticed. 106
  entries for this site, including the lightbox and the hover handlers that had
  gone missing.
- `bin/visual-check.py` — drives Chromium and *performs* each feature: clicks,
  hovers, and asserts the page changed. A handler that binds and does nothing
  passes every static check ever written. **98/98 on the clone.**

### BS-016 · Client scripts were created, linked, and never rendered · **done**

The root cause of both the missing hover menu and the dead lightbox, and a
textbook silent success. Page JS was carried into `Builder Client Script`
records — the apparently native home — created correctly and linked correctly to
each page. Builder emits the client-script include **only from a block whose
element is `body`**, and a Builder page rooted on a `div` never has one. Every
layer reported success and nothing ever executed.

The source keeps its scripts inside `</head>`, so they now go in the page's
`head_html`, which is both simpler and what the source actually does. Recorded
as part of TRAP-014's family: *native-looking is not the same as working.*

### BS-017 · Assets were only collected from `<img src>` · **done**

The hero background lived in `background-image: url(…)` and the favicon in
`<link rel="icon">`. The CSS rule was recovered perfectly and pointed at a file
that was never downloaded. The collector now reads media elements, `srcset`,
icon links, and `url()` anywhere in CSS.

### BS-018 · The new checker's own false positives · **done**

`visual-check` reported the favicon missing (it only looked at `<img>` and
backgrounds, not `link[rel=icon]`), and two headings missing (`Tea &amp;
Toast` stored escaped, compared against a browser's unescaped `innerText`). It
also called hover "inconclusive" because it tested the first link, which is the
logo and has no hover — while the real nav hover worked identically on both
sides. **A checker that reports inconclusive in the reassuring direction is worth
less than no checker**, so it now tries every candidate and fails loudly.

### BS-019 · The Builder pin was wrong — live was newer · **done**

Found by accident while chasing a single stray CSS rule, which is the wrong way
to find it.

The live site serves `/assets/builder/reset.css?v=6`. The pinned commit's
template says `?v=5`. Upstream bumped it in `04aeee5c` (2026-07-03), and our pin
`15cb01e4` (2026-07-02) is an **ancestor** of that commit — so live is running a
newer Builder than the sandbox.

**Why the first answer was wrong in a believable way.** The original probe
identified the commit by hashing the on-disk tree and reported *433 of 435 blobs
matched*, dismissing the two that did not as a rebuilt `yarn.lock` and a
`__pycache__` entry. That is a match *rate* — the same class of mistake as
BS-015. The residue was the answer.

**What it invalidates:** `trap-check`, `simulate-check` and `roundtrip-check` all
report results "at the pin", so all three are currently claims about a Builder
the target does not run. The trap findings are probably still correct — none of
them touch `reset.css` — but "probably" is not what those checks exist to
produce.

**Also worth noting:** ADR-004 exists entirely to say "never trust a version
claim, verify against the pin" — and nothing verified the pin itself against the
live site. The rule was written and then not applied to its own subject.

**Fixed.** Re-derived: Builder `b09a40d9` (2026-07-20) and frappe 16.27.1
`f33ac3f0`. The old answer was out by **55 commits and 17 days**, and against it
the real figures were **87 files differing, 69 host-only** — not "2 of 435".

Corroborated against non-tree evidence this time: the host's `LICENSE` is MIT and
Builder relicensed from AGPL-3.0 on 2026-07-16, which disproves the old pin on
its own. The rebuilt sandbox emits the same compiled bundle hashes the live site
serves (`index-B6GuYNDY.js`, `PageBuilder-Ca8AHlJ-.js`), and `reset.css?v=6` now
matches. All four sandbox checks pass at the new pin, and `buildsmith verify` went from
one persistent difference to **zero** — that difference was the pin.

Three things came out of the correction:

- **TRAP-016** — commit dates lie. `f0781da9` has an *earlier* committer date
  than our pin and is **not** an ancestor of it; the rename series shares one
  rebased timestamp. ADR-004 reasoned from dates while arguing against trusting
  version strings.
- **`init.sh` never migrated after a pin move.** New code against an old schema
  fails on the first field it expects. It now migrates unconditionally — the
  first attempt only migrated "if the pin moved this run", which misses a run
  that moved the code and then failed before migrating. Exactly what happened.
- **`roundtrip-check` was contaminated.** `publish-verify` puts tokens on the
  scratch site, so "tokens arrived" was true for the wrong reason and nearly
  produced a false *"upstream fixed TRAP-013"* conclusion. The bug is not fixed
  — the lookup is byte-identical at the new pin and exports zero files. The
  check now starts from an empty scratch site.

### BS-020 · No obvious way into the Builder editor on the dev instance · **fixed, 2026-08-04**

Raised by the owner. The dev instance does have working credentials —
`Administrator` / `admin` at `/login`, then `/builder` for the editor or `/app`
for the desk — but nothing said so anywhere.

The trap underneath it, and the reason "just try logging in" did not resolve it:
a successful login redirects to `Website Settings.home_page`, which for a cloned
site is the site's own home page. So signing in **correctly** looks exactly like
nothing happening, and there is no cue that the editor is somewhere else.

Fixed: `buildsmith sandbox status` prints the URLs, the credentials (read from
`pins.env`, so they cannot drift from what the sandbox was built with) and the
log-in-first ordering, every time it runs.

### BS-021 · The audit could not see a file until it was tracked · **fixed, 2026-08-04**

Found while writing `tests/test_audit.py`. The tree scan enumerated files with
`git ls-files`, which lists the **index** — so a file that had been written but
not yet `git add`ed was invisible. `buildsmith audit` answered "No findings" for
a tree that contained one.

Why the pre-commit guard did not cover it: the guard checks **tokens** and
RFC1918 addresses. The identifying-*fact* patterns — domains, emails, phone
numbers, street addresses — exist only in the audit. So a new file containing a
client's domain but not their name passed both: the audit never read it, and the
guard has no pattern for it. That is precisely the "facts leak too" case the
audit was written for.

Demonstrated concretely: a client-looking domain in the brand-new
`tests/test_audit.py` was reported clean by `buildsmith audit`.

Fixed: the scan enumerates `git ls-files --cached --others --exclude-standard`,
which is exactly the set of files that would be committed. Ignored files stay
out — they are the private layer, by design.

Two smaller audit bugs surfaced immediately afterwards, once it could see more:
the URL rule flagged `http://bench` (a dotless container service name cannot be
a public domain) and `http://203.0.113.10` (the RFC 5737 documentation range,
which the address rule already exempted but the URL rule did not consult). Both
fixed; both were pure noise, and noise is how an audit stops being read.

### BS-022 · The clone was not a valid Builder document · **fixed, 2026-08-05**

Found by the owner opening the editor: `/menu` rendered correctly but the same
page in `/builder/page/...` had no fonts and sat squished into a narrow
left-aligned column. "This is supposed to be a WYSIWYG editor."

**Two independent causes, one shape of mistake** — writing values into Builder's
fields that Builder's own UI would never produce.

**1. The document skeleton became blocks.** The converter's rule was "if there is
a `<body>`, take its children". **Frappe Builder's own page template emits no
`<body>` tag** — `<html>`, `<head>`, content, `</html>`. `html.parser` reports
only tags that literally appear and does not synthesise the implied body, so the
lookup returned `None`, the guard silently did nothing, and `<html>`/`<head>`/
`<title>` became the first blocks of every page. An unstyled `<html>` root
shrink-wraps in the editor canvas; a browser tolerates it in the published page,
which is why nothing downstream noticed.

**2. `font-family` held a CSS stack.** Builder treats it as one family name. Its
renderer copes — `get_google_font_urls` splits and takes the first. Its editor
does not: `fontManager.ts` does `encodeURIComponent(font)` on the whole value, so
`Skybald, Merriline, cursive` becomes `family=Skybald%2C%20Merriline%2C%20cursive`,
which Google Fonts rejects. The font silently failed to load **in the editor
only**.

Fixed: `_unwrap_document()` prefers `<body>`, falls back to unwrapping `<html>`,
drops `<head>`, carries `<title>` out to `page_title`, and *reports* the rewrite
instead of doing it silently. `_primary_font_family()` reduces stacks, applied to
both class rules and inline `style` attributes — the inline path wins the
specificity merge, so leaving it out would have undone the fix for exactly the
elements carrying the most deliberate typography.

**Verified:** all three pages now root on `div` with zero skeleton blocks; the
editor loads the display font (0 failed font requests, was 4) and fills the canvas.

### BS-023 · Validation compared rendered output, not editability · **root cause of BS-022**

`clone-diff` compares rendered HTML and CSS. `visual-check` drives a browser
against the rendered page. Both compare the clone's **published output** to the
source, and by that measure the clone was genuinely faithful — "zero content
differences, 98/98 features" was true. **Neither has ever opened the editor**, so
neither could see that the document was structurally invalid.

This is BS-015 one level up. Then it was *counts instead of content*; here it is
*rendered output instead of editability*. Both times the check measured the thing
that was easy to measure rather than the thing that mattered.

Partly addressed: `tests/test_replicate.py::BuilderNativeShapeTest` asserts on the
**block tree** rather than on rendered output, and would have caught both bugs
(verified — the pre-fix logic fails 5 of them). Still open: nothing yet opens the
editor as part of `verify`. That check needs to exist.

### BS-024 · Capture silently dropped a third of the design tokens · **fixed, 2026-08-05**

Found by the transport-equivalence test going red the moment real data was
loaded — the bench and REST captures produced different content hashes.

`capture` keyed its token map by `variable_name`. Builder scopes variable names
per group, so names collide freely: the live site has **167 variables under 112
distinct names**. Keying on the name discarded **55 of them**, and *which* 55
depended on iteration order, which differs between the two transports.

Two consequences, both silent. `drift` compares the content hash, so an
unchanged site could report drift. `publish-verify` mints Builder Variables from
that map, so its rehearsal was building a site missing a third of its tokens —
and a missing token is invisible: `var(--uuid)` falls back to the literal and
the page looks almost right.

Fixed: keyed by UUID, which is unique by construction. `variable_name` is now
hashed explicitly so a rename still registers as a change. Captured count went
from 112 to 168.

The equivalence test earned its keep here. It was written to catch a transport
returning a subtly different *shape*; it caught a data-loss bug in code both
transports shared, because two implementations of the same job disagreeing is a
signal even when neither is the one at fault.

### BS-025 · The audit could not see history's contents · **RESOLVED 2026-08-07 — history rewritten**

Found the moment the client-token list was completed. `buildsmith audit`
reported the tree clean and **history clean**, while 20 commits carried a client
token in tracked file content.

The history scope scanned commit *messages* and *paths*. Never *blob content*.
That is the shape of leak that survives every cleanup: the working tree is
spotless, every filename is neutral, and the token sits in the object store.

Worse, item 2 of "Before creating the remote" reads `buildsmith audit --scope
all` clean as the go-ahead to publish. It was not sufficient.

**Scanner fixed:** the history scope now reads every unique blob (deduplicated
by SHA, text only, 2 MB cap) and reports which ones carry a token. It
immediately found **17 blobs across two different clients** — the second one had
not been noticed at all.

**Still open, and it blocks the remote.** The tracked tree is now clean, but
history is not, and history is what gets read when a repo is made public. Two
remedies:

1. **Rewrite history** (`git filter-repo`) to purge the strings, then re-run the
   audit. Keeps the commit narrative, which is substantial here.
2. **Start the public repo from a squashed root commit.** Loses the narrative,
   guarantees the object store is clean, and cannot be got wrong.

Owner's call. Until then the pre-push hook refuses every push, which is correct.

**Resolution (2026-08-07): remedy 1, executed and doubly verified.** The owner
chose filter-repo. `git gc --prune=now` first removed four dangling blobs (one
carried the complete token roster on a single line — the worst object in the
store); `git filter-repo --replace-text` then replaced the remaining client
slug with `<site>` in the fourteen reachable blobs. Verified independently
twice: `buildsmith audit --scope all` clean (every unique blob, message, path
and ref), and a gitleaks sweep of the full rewritten history, also clean. The
pre-rewrite bundle and the old→new commit map live in the private backup
location; the bundle contains the pre-rewrite history *with* tokens, so the
"move backups to encrypted offline media" item inherits its sensitivity.
Operational notes for any future rewrite: filter-repo silently drops the
`origin` remote (re-add it), and a live linked worktree rides through safely
only when its branch is zero commits ahead — coordinate with the session
holding it before touching refs.

### BS-026 · Nothing scanned for generic credentials · **fixed, 2026-08-07**

**Evidence.** The guard, the audit and the pre-push sweep all check for
*client-identifying facts* — tokens, private addresses, PII shapes. None of
them was ever taught what a credential looks like. An AWS key, an API token or
a `BEGIN PRIVATE KEY` block pasted into a tracked file passed every gate this
repo had, because every pattern list was written for a different problem.

**Fix (ADR-010).** gitleaks now runs beside the guard, at both boundaries:
staged diff at pre-commit, full history at pre-push and in CI. A missing
binary exits 2 and refuses — an unscanned commit must never look scanned.
Reviewed false positives go in `.gitleaks.toml`, in a commit, so the review
is visible. Both scans were clean at adoption (60 commits, full tree), so the
allowlist starts empty. `install.py --dev` reports an absent binary at
install time; `buildsmith secretscan [--history]` runs it by hand.

### BS-027 · Older docs predate the plain-language standard · **open**

CONTRIBUTING.md sets the writing bar: roughly an eighth-grade reading level,
plain words, jargon defined at first use, welcoming to FOSS/STEAM peers who
are not this repo's authors. The docs written or rewritten alongside it
measure at or near that bar (Flesch-Kincaid: README 8.7, CONTRIBUTING 6.7,
SECURITY 8.4). The docs that predate it do not: AGENTS.md 11.6,
docs/ROADMAP.md 11.2, docs/going-public.md 9.0, and most ADRs sit in the
10–12 range.

Not blocking, and not a mechanical rewrite: AGENTS.md and the ADRs are
engineering records whose precision is the point, and several are pinned by
tests. The right move is a considered pass per document — shorten sentences,
define terms, keep the precision — plus holding every *new* doc to the bar.
(CODE_OF_CONDUCT.md is exempt: it is the Contributor Covenant verbatim, and
adopted standards should not be paraphrased.)

### BS-010 · Fixture directories accumulate · **open**

Because a page's name is an unchooseable hash (TRAP-012), re-authoring the same
page mints a new `builder_files/pages/<hash>/` directory while the old one
remains and keeps importing — two published pages racing for one route. Anything
built on that layer needs an explicit prune step. Not blocking: we are not
shipping on it yet.

---

## Before creating the remote

1. [x] BS-001 closed — the roster was completed on the operations side
       (BS-025 records the audit running against the completed list; it is
       non-empty and covers every engaged client).
   - [x] BS-002 closed.
   - [x] BS-004 wired to a pre-push hook.
2. [x] `buildsmith audit --scope all` clean, with a **non-empty** token list — an
       empty list makes the token half of the audit prove nothing, and the audit
       says so rather than reporting success.
       **Includes history's CONTENT since BS-025.** A clean tree over dirty
       history is the leak that survives every scrub, and this item read as a
       publish go-ahead while 17 blobs carried two clients' names.
       **Verified clean 2026-08-07, post-rewrite** — re-run immediately before
       the first push; it is the pre-push hook's job anyway.
   - [x] BS-025 history remediated (filter-repo, 2026-08-07; audit + gitleaks
       both clean over the rewritten history).
3. [x] A human read-through for *facts*, per BS-003. **Done 2026-08-07 with
       the owner**: two independent full-tree sweeps; the identified
       fingerprint cluster (font pair, menu copy, route pair, feature trio)
       was genericised, and the bootstrap narrative demoted to a private
       working file.
4. [x] Confirm the private layer is still untracked: `git ls-files sites/`
       should list only `sites/example/`. **Verified 2026-08-05.** Re-check
       immediately before flipping public — this one can regress with a single
       `git add -f`, which is why the guard also refuses it at commit time.
5. [x] Visibility decided by the owner, 2026-08-07: **public from day one** —
       the gate above was completed first, so the private soak period was
       consciously skipped.
6. [x] `buildsmith secretscan --history` clean (BS-026 / ADR-010; verified
       repeatedly 2026-08-07, and re-run by the pre-push hook) — generic
       credentials are a different class from client facts, and history is what
       gets read once the repo is public.
7. [ ] The forge-side setup applied in order: CI secret, `main` ruleset,
       GitHub secret scanning + push protection. The runbook with the exact
       commands is `docs/going-public.md`.
