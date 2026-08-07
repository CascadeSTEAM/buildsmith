"""Synthetic identifying values, assembled rather than written literally.

The tests for `buildsmith audit` and `buildsmith guard` need values that *look*
like real findings — a client domain, an email, a phone number, a street. If
they did not, the tests would be vacuous: a scanner asserted only against
documentation-reserved values proves nothing about the values it exists to
catch.

But this repo is published, and the audit reads every tracked *and untracked*
file. A client-looking domain written literally in a test file is a domain in
the repo, and it would be found by the very scanner it is testing — correctly.
That finding would then appear on every run, and an audit that always reports
something is an audit people stop reading.

(The first draft of this very docstring spelled the fixture domain out while
explaining why not to. The audit caught it. That is the mechanism working.)

So the fixtures are assembled at import time. The scanner's line-oriented
patterns see `"client" + "corp"`, which matches nothing, while the tests see the
joined string. This is the same idiom `tests/test_publication_guard.py` uses for
its RFC1918 address, stated once here instead of re-invented per file.

None of these name a real business. They are deliberately generic.
"""

from __future__ import annotations

#: A client-looking domain on a real public suffix, so it is *not* exempt.
CLIENT_DOMAIN = "client" + "corp" + "." + "com"

#: Tool-vendor addresses, which the audit exempts only in an authorship trailer.
VENDOR_EMAIL = "@".join(("noreply", "anthropic" + ".com"))
FORGE_EMAIL = "@".join(("adev", "users.noreply." + "github" + ".com"))

#: A person at a client. Must never be exempt, wherever it appears.
CLIENT_EMAIL = "@".join(("a.person", CLIENT_DOMAIN))

#: NANP reserved-for-fiction range, still formatted like a real number.
CLIENT_PHONE = "(503) " + "555-0100"

#: A street address that contains no documentation-reserved word.
CLIENT_STREET = "1234 " + "Maple Street"

#: Hosts used to prove `frappe_client` refuses a real deployment.
PUBLIC_HOST = "builder." + "example" + ".com"  # reserved, but still not local
LIVE_HOST = "acme" + ".com"
LIVE_CLOUD_HOST = "acme." + "frappe" + ".cloud"
#: A near-miss on the allow-list: an allowed name as a *prefix* of a real one.
NEAR_MISS_SITE = "sandbox.localhost." + LIVE_HOST
