---
name: builder-golive
description: Go-live choreography — generates the command plan and verification checklist; execution belongs to the operations project
mode: skill
triggers: go live,golive,launch,cutover,dns,domain,publish site
---

# builder-golive

**Choreography only.** This produces a plan and a checklist. Every step that
touches a live system — DNS, reverse proxy, Frappe domains, certificates — is
executed by a subagent in the operations project, using its own roles and
playbooks (`configure-cloudflare-dns`, `caddy`, `frappe-site`). Buildsmith does
not own any of it.

**Load `builder-safety` first**, then the operations project's `frappe-access`.

## Order

1. **Content complete.** Every route present, verified against the source.
   `ReplicateResult.coverage` is 1.0, or the theme build has no warnings.
2. **Template published.** If it has a `template_group`, developer_mode must be
   on for the write and off again afterwards — and expect fixture files to
   appear in the app directory on the host (TRAP-006).
3. **Routes verified one at a time.** Static routes shadow dynamic ones. Build
   the dynamic page and confirm each record renders **before** retiring the
   legacy page. Retiring first 404s live URLs; renaming breaks inbound links
   (TRAP-010).
4. **Scheduler and workers up** before anything that publishes. `queue_action`
   with no worker locks documents permanently (TRAP-009).
5. **DNS and proxy** — operations project.
6. **Verify** every route over the public hostname, not just locally.
7. **Journal the run** and attach the build log to the ticket.

## Before you start

- Backup and a `Builder Snapshot`. Both.
- A clean `buildsmith simulate` run for every component payload.
- Check the timezone. A blank `System Settings.time_zone` stores timestamps in
  IST, which has already caused a stale probe to be trusted (TRAP-008).
