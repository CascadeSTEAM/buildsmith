# CLAUDE.md

See **`AGENTS.md`** — it is the source of truth for working in this repo, kept
vendor-neutral so Claude Code, OpenCode, and humans share one document. This file
exists only because Claude Code looks for `CLAUDE.md` specifically; its content is
not duplicated here so the two cannot drift.

**Start with `docs/ROADMAP.md` and `docs/ISSUES.md`** — the plan and the open
items. (`RESUME.md`, if present, is a gitignored private working file: the
bootstrap-era session narrative. Public clones do not have it.)

**Most important rule:** any access to a live system (SSH, Frappe bench, DNS,
reverse proxy, installs, secrets, writes against a live Frappe instance) is performed
by a **subagent in the OpsKit project** (`~/Projects/opskit`) — never directly from
this repo — and is **offered proactively** whenever such work comes up. Full policy
and the actions-vs-artifacts boundary: `AGENTS.md`.
