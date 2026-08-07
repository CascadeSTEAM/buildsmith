# Security Policy

Buildsmith takes safety seriously. That covers its own code, the client
websites it gets pointed at, and the private data it is designed to keep out
of this repo. If you found a way to break any of that, thank you for reading
this first.

## Reporting a vulnerability

**Please don't open a public issue for a security problem.**

Instead, use GitHub's private vulnerability reporting: the **Security** tab
of this repository → **Report a vulnerability**. That opens a private thread
with the maintainer. Nothing is visible to anyone else until a fix is out.

You can expect an acknowledgment within a few days. This is a community
project, not a company with an on-call rotation. Even so, security reports go
to the top of the pile. There's no bounty program. There *is* public credit
in the fix, with your permission.

## What counts as a vulnerability here

The usual things count: code execution, injection, and friends. Buildsmith
also makes two promises that are security properties in their own right.
A way to break either one is a vulnerability, even with no "exploit"
involved:

1. **Tools emit files — they never touch a live site.** No tool in this repo
   should be able to write to any website outside its two-name local
   allow-list (`LOCAL_ONLY`). If you find a code path, flag, or trick that
   reaches a real site, report it.
2. **The gates fail closed.** The publication guard, the secret scan, and
   the pre-push audit must refuse when they cannot run. If you can make a
   gate pass *silently* — it skips without saying so, or an input crashes it
   into success — report that too. A quiet no-op is exactly the failure
   these gates exist to prevent.

One more: if you find a real leaked credential or private detail anywhere in
this repository or its history, report it privately. We'll rotate and scrub
before anything else.

## Supported versions

Buildsmith is pre-1.0. Fixes land on `main`; there are no maintained release
branches yet. If that changes, this table will say which versions get
patches.
