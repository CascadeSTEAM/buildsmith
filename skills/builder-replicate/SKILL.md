---
name: builder-replicate
description: W1 — faithful, complete copy of an existing site into Builder, routes preserved
mode: skill
triggers: replicate,crawl,migrate site,copy site,import site,scrape
---

# builder-replicate

W1. Spin up an instance and get a **faithful, complete** Builder copy of an
existing site, fast. A productized service, not a bespoke redesign tool — if you
find yourself improving the design, you are in `builder-theme` instead.

**Load `builder-safety` first.**

## The success floor

All original content present · site navigable · **routes preserved**. Not
"looks nicer". A replication that silently drops a page is the failure worth
engineering against, because it looks exactly like one that worked.

```sh
python3 -c "from workflows.replicate import crawl_site, replicate, emit; \
  c = crawl_site('https://example.test'); r = replicate(c, site='<site>'); \
  print(r.summary()); emit(r, 'sites/<site>/build')"
```

Read the summary. **Coverage under 100% means an incomplete copy** and it says
so. So does a truncated crawl.

## What it deliberately does not carry

- `<script>` and `<style>` — behaviour and presentation, not content. A copied
  script would carry the source site's analytics into the new site, and would
  not work re-hosted anyway.
- Framework attributes (`ng-*`, `onclick`, `data-reactroot`) — they describe
  behaviour that will not exist in Builder, and copying them produces markup
  that looks meaningful and is inert.

Everything dropped is counted and reported. Read that list before declaring the
replication complete.

## Still emits a template

No exceptions. A replicated site that cannot be maintained afterwards is a
delivery, not a service.

## Known limits

SPA and Quartz-style sources replicate poorly — the content is assembled by
JavaScript that this deliberately does not run. That is a documented exception,
not a defect. Static and marketing sites replicate well.

Crawling honours `robots.txt` by default. Override only for a site the client
controls, and make that an explicit decision.
