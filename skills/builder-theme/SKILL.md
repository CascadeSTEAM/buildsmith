---
name: builder-theme
description: W2 — evolve a site's design system: token manifest, components, template, data-driven pages
mode: skill
triggers: theme,tokens,design system,palette,components,template,restyle
---

# builder-theme

W2. Evolve a design system over time. This is **not** the workflow for copying
an existing site — that is `builder-replicate`, and merging the two is the drift
this project was restarted to avoid.

**Load `builder-safety` first.** Then the operations project's `frappe-access`.

## Order, and why it is not negotiable

1. **Tokens.** Diff the manifest against the live map and apply the plan.
2. **Read the token map back.** Composition embeds each token's *live* value as
   the fallback in `var(--uuid, literal)`, so composing against a lagging map
   writes yesterday's colours into every component.
   `Applied.assert_in_sync()` refuses to let you.
3. **Components.** Compose or revise. A revision must preserve blockIds.
4. **Template.** Mandatory, no exceptions.
5. **Pages.** Data-driven records last.

```sh
buildsmith build --site <site>      # emit payloads to sites/<site>/build/
buildsmith validate --site <site>
buildsmith simulate --state <export> --payload sites/<site>/build/components/<c>.json
buildsmith handoff --site <site>
```

## The constraints worth memorising

- **Only `Color` and `Dimension` are tokens.** Font families, weights, unitless
  line-heights, shadows and easing cannot be — they become a component prop plus
  one injected `head_html` stylesheet (TRAP-004).
- **Never choose a token's name.** Builder assigns a uuid, and upstream rewrites
  any non-uuid name on the next migrate. Component ids are the opposite — those
  are yours, and should be readable.
- **Never pre-compose `light-dark()`.** Builder composes it from `value` and
  `dark_value` when they differ.
- **Intent and applied are different files** and are never merged. The manifest
  is what the design says; the applied map is what is live.
