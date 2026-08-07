# W1 · Replicate — clone a live site, work on it locally, publish it back

The loop this supports:

> Clone the live site into a local dev instance → edit and test against that →
> approve it → publish back to live, merging or overwriting deliberately.

The dev instance is the working surface, not a demo. That is why every step
below either proves something or refuses to continue.

---

## The sequence

```sh
buildsmith sandbox up                                  # once: the pinned Builder dev instance
python3 install.py --dev                             # once: Chromium for the browser checks

buildsmith clone --site acme --source https://acme.test/
buildsmith verify --site acme --source https://acme.test/
```

Then **edit the site** at `http://127.0.0.1:8000/builder`
(`Administrator` / `admin`), and when you are happy:

```sh
buildsmith capture --site acme                        # read your edits back out
buildsmith drift --site acme --source https://acme.test/  # did live move under you?
buildsmith publish-verify --site acme                 # rehearse the publish on a scratch site
buildsmith handoff --site acme                        # hand the proven payload over
```

Applying to the live site is an **action**, so it is performed by an operations
subagent — never from this repo. `buildsmith handoff` prints what they need.

---

## What each step proves, and what it refuses

### `buildsmith clone`

Crawls every page (same origin, `robots.txt` honoured), downloads every asset,
converts HTML into Builder blocks, extracts the feature inventory, and loads it
into the dev instance.

**Refuses:** an empty crawl, a page that converts to nothing, a site with no
template. **Reports rather than hides:** route coverage below 100%, a truncated
crawl, and every fragment not carried across.

### `buildsmith verify` — the one that matters

Two checks, because they prove different things:

- **`clone-diff`** — set differences on selectors, per-selector declarations,
  assets, text, links and scripts. Not counts. *375 of 376 declarations can be
  375 different declarations*, and a count cannot tell those apart.
- **`visual-check`** — drives a browser and **performs** every feature in
  `features.json`: clicks the thing, hovers the thing, asserts the page changed.
  A handler that binds and does nothing passes every static check ever written.

`features.json` is **extracted from the source, never hand-written**. A written
checklist contains the features somebody remembered; the ones that go missing
are always the ones nobody noticed.

### `buildsmith capture`

Reads the dev instance back — pages, components, the applied token map, the home
page setting — into `sites/<site>/dev-state/`, with a content hash.

You edited in Builder, so the dev instance is the truth. Rebuilding from our
inputs instead would quietly discard your work.

### `buildsmith drift`

Re-fetches the live site and compares it against the crawl the clone came from.
Answers the one question the payloads cannot: **has live moved since we copied
it?** If it has, an overwrite reverts somebody's work and a merge is guesswork.

An unreachable source is reported **as drift**, not as absence of drift.

### `buildsmith publish-verify` — the rehearsal

Applies the captured state to a scratch site *from empty*, then checks the
scratch site against dev with both tools above.

The asymmetry this exists for: a cloning mistake is recoverable, because live is
untouched and you notice by looking at localhost. A publishing mistake is
destructive and you hear about it from a customer. If the rehearsal does not
reproduce dev, the payload would have produced the same wrongness on the real
site, silently.

---

## Known limits, stated rather than discovered

- **JavaScript-assembled content does not replicate.** Content a script fetches
  at runtime is not in the HTML we crawl. Static and marketing sites replicate
  well; SPA sources do not. Documented exception, not a defect.
- **Third-party and analytics scripts are never carried.** The site's own
  behaviour is (into `head_html`, where the source keeps it). External `src`
  scripts and anything matching a tracker pattern are refused and reported.
- **Descendant and pseudo selectors are not folded onto blocks.** `.a .b` cannot
  be attributed to one element, and guessing would silently restyle the wrong
  one. They go to `head_html` intact.
- **The clone is a snapshot.** Run `buildsmith drift` before publishing, every time.

## If something is missing from the clone

Do not reach for the converter first. Run `buildsmith verify` and read what it says —
it names the specific selector, asset, text run or feature. Every omission found
so far has been in one of four places: an asset referenced somewhere other than
`<img src>`, a rule that could not become a block style, a script that was
carried but never rendered, or a check that measured the wrong thing.
