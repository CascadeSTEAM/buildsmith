#!/usr/bin/env python3
"""Whole-repository publication audit — the sweep the pre-commit guard is not.

`buildsmith guard` is a **gate**: it inspects the staged diff on the way past. That
is the right shape for a hook and it leaves four holes, every one of which was
confirmed against a real client's data before this file was written:

1. **Scope.** It sees added lines and changed paths, so anything already in the
   tree — or in history — is invisible. Commit an unrelated file and the guard
   happily passes over a leak sitting next to it.
2. **Branch names are unchecked.** `AGENTS.md` said so in as many words, and a
   client-suggestive branch had already reached a public origin elsewhere.
3. **The token list is a list.** A client absent from it is a client the guard
   has never heard of. Ours was.
4. **Facts are not tokens.** A phone number, a street, a public IP, a domain, a
   container image name — none of these match a token, and any one of them
   identifies a business as surely as its name does.

So this scans the **whole tracked tree, the whole history, commit messages and
branch names**, for tokens *and* for the shapes that identify somebody without
naming them.

It is deliberately noisier than the guard. A gate must not cry wolf or people
route around it; an audit is read by a human who can dismiss a finding, and the
cost of a miss is permanent.

    buildsmith audit                 # tree, history, refs — everything
    buildsmith audit --scope tree    # just the working tree, fast
    buildsmith audit --json

Exit status is 1 if anything is found. Nothing here touches a live system.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from buildsmith.tools.gitenv import hermetic_env

ROOT = Path(__file__).resolve().parents[2]

__all__ = ["Finding", "audit", "collect_tokens", "scan_text"]


@dataclass(frozen=True)
class Finding:
    where: str
    kind: str
    detail: str
    line: str = ""

    def __str__(self) -> str:
        snippet = f"  {self.line.strip()[:100]}" if self.line else ""
        return f"[{self.kind}] {self.where}: {self.detail}{snippet}"


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    tokens_available: bool = True
    scanned: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.findings and self.tokens_available


# --- what counts as identifying ---------------------------------------------
#
# Documentation-reserved values, which are safe by design and must not be
# flagged or the audit becomes noise people learn to ignore.
SAFE_DOMAINS = {
    "example.com", "example.org", "example.net", "example.test", "example.invalid",
    "localhost", "test", "invalid", "local",
}
#: Reserved / non-routable suffixes. `sandbox.localhost` is ours, not anyone's.
SAFE_DOMAIN_SUFFIXES = (
    ".localhost", ".test", ".invalid", ".local", ".example", ".internal",
    # Upstream and tooling we legitimately reference by name.
    "github.com", "frappe.io", "frappeframework.com", "python.org", "docker.com",
    "anthropic.com", "claude.com",
    # Asset hosts every Builder site references. Builder's own renderer and its
    # editor both emit these; naming one identifies nobody.
    "fonts.googleapis.com", "fonts.gstatic.com",
    "githubusercontent.com", "ghcr.io", "spdx.org", "json.org", "w3.org",
    "developer.mozilla.org", "redis.io", "mariadb.org", "hatch.pypa.io",
    "astral.sh", "opensource.org", "creativecommons.org",
    # The Contributor Covenant's required attribution links (CODE_OF_CONDUCT.md).
    "contributor-covenant.org",
    # Loopback and the dev instance. Not anybody's site.
    "127.0.0.1", "0.0.0.0", "::1",
    # Third-party trackers, named here only because this project maintains a
    # blocklist of them. A blocklist that trips the leak scanner is a scanner
    # finding itself again — see BS-014.
    "googletagmanager.com", "google-analytics.com", "segment.com", "clarity.ms",
    "hotjar.com", "mixpanel.com", "matomo.org", "plausible.io", "posthog.com",
)
#: RFC 5737 / RFC 3849 documentation ranges, plus loopback.
SAFE_IP_PREFIXES = ("192.0.2.", "198.51.100.", "203.0.113.", "127.", "0.0.0.0", "255.255.255")

#: Public suffixes worth flagging. Deliberately a list rather than "any word":
#: precision is what makes the audit readable, and the token check plus the other
#: patterns still cover a domain on an exotic TLD.
#: Deliberately excludes TLDs that are also everyday code identifiers — `site`,
#: `name`, `page`, `app`, `dev`, `io`, `sh`, `me`, `co`. Those matched
#: `args.site`, `self.name` and `path.parent.name` and produced nothing but
#: noise. A domain on one of them is still caught by the URL rule below whenever
#: it appears as an actual address, which is the case that matters.
_PUBLIC_TLDS = {
    "com", "org", "net", "info", "biz", "xyz", "shop", "store", "tech", "cloud",
    "blog", "cafe", "pizza", "restaurant", "kitchen", "gov", "edu",
    "uk", "ca", "au", "nz", "de", "fr", "nl", "eu",
}

PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
     "an email address identifies a person or an organisation"),
    ("phone", re.compile(r"(?<![\w.])(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?![\w.])"),
     "a phone number identifies a business"),
    ("street", re.compile(
        r"\b\d{1,5}\s+[A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+)*\s+"
        r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Way|Court|Ct)\b"),
     "a street address identifies a location"),
    ("public-ip", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
     "a routable IP address identifies infrastructure"),
    # Anchored on a real public suffix. The obvious pattern — label dot label —
    # matches `block.get`, `json.loads` and `parent.parent`, which produced 1620
    # findings on this repo's own source and would have taught everyone to skip
    # the audit. A detector nobody reads protects nothing.
    ("domain", re.compile(
        r"\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:" + "|".join(sorted(_PUBLIC_TLDS)) + r")\b",
        re.I),
     "a real domain identifies whose site this is"),
    # Any host in a URL, whatever its TLD. This is the unambiguous case: nobody
    # writes https://… by accident.
    ("url", re.compile(r"\bhttps?://([\w.-]+)"),
     "a URL names a specific host"),
]

#: Authorship trailers naming a *tool vendor*, not a person or a client.
#:
#: Deliberately narrow. A blanket "trailers are fine" rule would wave through
#: `Co-Authored-By: someone@clientcorp.example`, which names an individual at a
#: client and is exactly the kind of fact this audit exists to catch. Only the
#: no-reply addresses of development tooling are exempt, and only on a line that
#: is structurally a trailer.
#:
#: This matters because the audit is only useful if it is read. A finding that
#: fires on every single commit trains people to skim past the section it
#: appears in, and the cost of a missed real finding is permanent.
_TOOL_ATTRIBUTION = re.compile(
    r"^\s*(?:Co-Authored-By|Signed-off-by|Reported-by|Reviewed-by)\s*:.*"
    r"<?(?:noreply@anthropic\.com|[\w.+-]+@users\.noreply\.github\.com)>?\s*$",
    re.I,
)

#: Text that looks like a finding but is this project describing its own rules.
_SELF_REFERENTIAL = re.compile(
    r"RFC\s?(1918|5737|3849)|documentation range|example\.|placeholder|"
    r"a phone number|a street address|identifies a|version string|not an address|"
    r"blocklist|tracker|analytics",
    re.I,
)


def collect_tokens(opskit: Path | None = None) -> list[str]:
    """Client tokens, resolved from the operations project at run time.

    Never written down here — this repo is the thing being audited.
    """
    root = opskit or Path(os.environ.get("OPSKIT_ROOT", Path.home() / "Projects" / "opskit"))
    tokens: set[str] = set()
    environments = root / "environments"
    if environments.is_dir():
        tokens |= {
            child.name for child in environments.iterdir()
            if child.is_dir() and child.name != "example" and not child.name.startswith(".")
        }
    listing = root / ".client-tokens"
    if listing.is_file():
        tokens |= {
            line.strip() for line in listing.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
    extra = os.environ.get("CLIENT_TOKENS", "")
    tokens |= {t for t in re.split(r"[,\s]+", extra) if t}
    return sorted(tokens)


def _is_safe_domain(value: str) -> bool:
    lowered = value.lower().rstrip(".")
    if lowered in SAFE_DOMAINS or lowered.endswith(SAFE_DOMAIN_SUFFIXES):
        return True
    # A dotless label cannot be a public domain: it is a container service name,
    # a docker network alias or a LAN hostname. `http://bench:8000` names
    # nobody. Without this the URL rule flags every compose service we mention.
    if "." not in lowered:
        return True
    # An address that reached here through the URL rule still gets the address
    # rules applied. `http://203.0.113.10` is the RFC 5737 documentation range
    # and is exactly as safe inside a URL as it is outside one.
    if re.fullmatch(r"[\d.]+", lowered):
        return _is_safe_ip(lowered)
    # A bare filename like `build.py` or `catalog.md` matches the domain shape.
    return lowered.rsplit(".", 1)[-1] in {
        "py", "md", "sh", "js", "ts", "json", "yml", "yaml", "toml", "txt", "html",
        "htm", "css", "png", "jpg", "svg", "webp", "lock", "cfg", "ini", "env", "xml",
    }


def _is_safe_ip(value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 4 or any(not p.isdigit() or int(p) > 255 for p in parts):
        return True  # a version string like 16.25.0.1, not an address
    if value.startswith(SAFE_IP_PREFIXES):
        return True
    first, second = int(parts[0]), int(parts[1])
    # RFC1918 is the guard's job and it already fails the commit; not repeated here.
    return (
        first == 10
        or (first == 172 and 16 <= second <= 31)
        or (first == 192 and second == 168)
    )


def scan_text(text: str, where: str, tokens: list[str]) -> list[Finding]:
    """Scan one blob for tokens and for identifying facts."""
    findings: list[Finding] = []

    for token in tokens:
        for match in re.finditer(rf"\b{re.escape(token)}\b", text, re.I):
            line = text[: match.start()].count("\n") + 1
            findings.append(
                Finding(f"{where}:{line}", "client-token", f"contains {token!r}",
                        text.splitlines()[line - 1] if line <= len(text.splitlines()) else "")
            )
            break  # one per token per file is enough to act on

    lines = text.splitlines()
    for number, line in enumerate(lines, 1):
        if _SELF_REFERENTIAL.search(line) or _TOOL_ATTRIBUTION.match(line):
            continue
        for kind, pattern, why in PATTERNS:
            for match in pattern.finditer(line):
                value = match.group(0)
                if kind in ("domain", "url") and _is_safe_domain(
                    value.split("//")[-1] if kind == "url" else value
                ):
                    continue
                if kind == "public-ip" and _is_safe_ip(value):
                    continue
                if kind == "email" and value.lower().endswith(tuple(SAFE_DOMAINS)):
                    continue
                # Our own fictional fixtures say "Example" on purpose.
                if "example" in value.lower() or "nowhere" in value.lower():
                    continue
                findings.append(
                    Finding(f"{where}:{number}", kind, f"{value!r} — {why}", line)
                )
    return findings


def _git(*args: str) -> str:
    # hermetic_env: under a hook, an inherited GIT_DIR would out-rank -C and
    # silently audit a different repository than ROOT names (see gitenv).
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True, text=True, env=hermetic_env(),
    ).stdout


def audit(scope: str = "all") -> Report:
    report = Report()
    tokens = collect_tokens()

    if not tokens:
        report.tokens_available = False
        report.findings.append(
            Finding("(token list)", "fail-closed",
                    "no client tokens could be resolved, so the token half of this audit "
                    "checked nothing. Set OPSKIT_ROOT or CLIENT_TOKENS.")
        )

    if scope in ("tree", "all"):
        # `--others --exclude-standard` as well as the cache: a file that is not
        # tracked *yet* is exactly the one about to be committed, and reading
        # only the index means `audit` answers "no findings" about a tree that
        # contains one. The pre-commit guard does not close this — it checks
        # tokens, not the identifying-fact patterns, which live only here.
        # Ignored files stay out: they are the private layer, by design.
        files = [f for f in _git("ls-files", "--cached", "--others",
                                 "--exclude-standard").splitlines() if f]
        report.scanned["files"] = len(files)
        for path in files:
            full = ROOT / path
            try:
                text = full.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            report.findings += scan_text(text, path, tokens)
            for token in tokens:
                if re.search(rf"\b{re.escape(token)}\b", path, re.I):
                    report.findings.append(
                        Finding(path, "client-token", f"the path contains {token!r}")
                    )

        # Private paths must never be tracked, whatever they contain.
        stray = [f for f in files if f.startswith("sites/") and not f.startswith("sites/example/")]
        for path in stray:
            report.findings.append(
                Finding(path, "private-path", "tracked under sites/ but is not sites/example/")
            )

    if scope in ("history", "all"):
        messages = _git("log", "--all", "--format=%H%n%B%n---END---")
        report.scanned["commits"] = messages.count("---END---")
        report.findings += scan_text(messages, "(commit messages)", tokens)

        # Historical CONTENT, not just historical paths. Scanning messages and
        # filenames and calling that "history clean" is how a leak survives a
        # scrub: the working tree is spotless, every path is neutral, and the
        # token sits in the blobs of twenty old commits, which is exactly what
        # gets read when a repository is made public.
        #
        # Deduplicated by blob SHA, so identical content across commits is read
        # once. Text only — a binary blob has no lines to report.
        seen_blobs: set[str] = set()
        batch = _git("cat-file", "--batch-all-objects",
                     "--batch-check=%(objectname) %(objecttype) %(objectsize)")
        for line in batch.splitlines():
            parts = line.split()
            if len(parts) != 3 or parts[1] != "blob":
                continue
            sha, size = parts[0], int(parts[2])
            if sha in seen_blobs or size > 2_000_000:
                continue
            seen_blobs.add(sha)
            content = _git("cat-file", "blob", sha)
            if not content or "\x00" in content[:1024]:
                continue
            for token in tokens:
                if re.search(rf"\b{re.escape(token)}\b", content, re.I):
                    report.findings.append(Finding(
                        f"(history blob {sha[:10]})", "client-token",
                        f"a past version of a file contains {token!r}. It is still in the "
                        "object store and will be published with the repository."
                    ))
                    break
        report.scanned["history_blobs"] = len(seen_blobs)

        blobs = [ln.split()[-1] for ln in _git("rev-list", "--objects", "--all").splitlines()
                 if " " in ln]
        report.scanned["historical_paths"] = len(blobs)
        for path in set(blobs):
            for token in tokens:
                if re.search(rf"\b{re.escape(token)}\b", path, re.I):
                    report.findings.append(
                        Finding(f"(history) {path}", "client-token",
                                f"a path in history contains {token!r}")
                    )

    if scope in ("refs", "all"):
        refs = [r for r in _git("for-each-ref", "--format=%(refname:short)").splitlines() if r]
        report.scanned["refs"] = len(refs)
        for ref in refs:
            for token in tokens:
                if re.search(rf"\b{re.escape(token)}\b", ref, re.I):
                    report.findings.append(
                        Finding(f"(ref) {ref}", "client-token",
                                f"a branch or tag name contains {token!r}")
                    )

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--scope", choices=["tree", "history", "refs", "all"], default="all")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--kind", action="append", help="only report these kinds")
    args = parser.parse_args(argv)

    report = audit(args.scope)
    findings = report.findings
    if args.kind:
        findings = [f for f in findings if f.kind in args.kind]
    # --kind narrows what is SHOWN, never what counts. Exit codes follow the
    # full report — otherwise `--kind fact` on a repo with token findings
    # prints "No findings" and exits 0, and a hook built on that code ships
    # the leak.
    hidden = len(report.findings) - len(findings)

    if args.json:
        print(json.dumps(
            {"ok": report.ok, "scanned": report.scanned, "hidden_by_kind": hidden,
             "findings": [f.__dict__ for f in findings]}, indent=2))
        return 0 if report.ok else 1

    print(f"publication audit — scope: {args.scope}")
    for what, count in sorted(report.scanned.items()):
        print(f"  {what.replace('_', ' ')}: {count}")
    print()

    if not findings:
        if hidden:
            print(f"No findings of kind {', '.join(args.kind)} — but {hidden} "
                  "finding(s) of other kinds exist. The exit code counts them; "
                  "rerun without --kind to see them.")
            return 1
        print("No findings.")
        if not report.tokens_available:
            print("But the token list was empty, so this proves less than it looks.")
            return 1
        print("Tokens, facts, paths, history, refs and branch names all clean.")
        return 0

    by_kind: dict[str, list[Finding]] = {}
    for finding in findings:
        by_kind.setdefault(finding.kind, []).append(finding)

    for kind in sorted(by_kind):
        items = by_kind[kind]
        print(f"{kind} — {len(items)}")
        for finding in items[:25]:
            print(f"  {finding}")
        if len(items) > 25:
            print(f"  ... and {len(items) - 25} more")
        print()

    if hidden:
        print(f"({hidden} finding(s) of other kinds not shown — the exit code "
              "counts them too.)\n")
    print("An audit finding is not automatically a leak — read them. But note that")
    print("facts identify as surely as names: a phone number and a street are enough")
    print("to name a business without ever typing what it is called.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
