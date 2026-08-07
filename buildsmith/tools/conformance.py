"""Is this a document Builder itself could have produced?

Every check here is static — no browser, no credentials, no running instance —
because the bug that motivated it (BS-022) was visible in the block JSON and we
looked at rendered output instead.

The distinction that matters: `clone_diff` and `visual_check` ask **"does the
published page look like the source?"**. Both answered yes about a clone whose
root block was `<html>`, whose `<head>` and `<title>` were blocks, and whose
`font-family` held a CSS stack Builder's editor cannot parse. The page rendered
correctly and was unusable in the editor.

So this asks a different question: **"would Builder have written this?"** A page
that fails here may still render perfectly. It is still broken, because the whole
point of the workflow is that someone opens it in Builder afterwards.

Rules are derived from the pinned Builder's own behaviour, not from taste:

- `blockTemplate.ts` emits `div` and nothing else, and every Builder-authored
  page in the sandbox roots on `div`. `<html>`, `<head>`, `<body>` and `<title>`
  are document skeleton — Builder owns the document, the block tree is content.
- `fontManager.ts` does `encodeURIComponent(font)` on the whole `font-family`
  value, so anything but a single family name yields a Google Fonts 400 and the
  font silently never loads *in the editor only*.

Exit codes follow the project contract: 0 proved · 1 problem · 2 could not check.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from buildsmith.errors import EXIT_OK, EXIT_PROBLEM, EXIT_UNCHECKED

#: Document skeleton. Never content, so never a block.
SKELETON = frozenset({"html", "head", "body", "title", "meta", "link", "script", "style"})

#: What a Builder page's block tree may root on. Builder's own template only ever
#: emits `div`; the others are accepted because a hand-authored page legitimately
#: roots on a semantic container and Builder renders them.
ROOT_ELEMENTS = frozenset({"div", "section", "main", "header", "footer", "article", "nav"})

STYLE_BUCKETS = ("baseStyles", "rawStyles", "mobileStyles", "tabletStyles")

__all__ = ["Finding", "Report", "check_blocks", "check_payload_dir", "main"]


@dataclass(frozen=True)
class Finding:
    where: str
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.where}: {self.detail}"


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    blocks_checked: int = 0
    pages_checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.findings


def _walk(block: dict, path: str = "") -> Iterator[tuple[str, dict]]:
    here = f"{path}/{block.get('element') or '?'}"
    yield here, block
    for index, child in enumerate(block.get("children") or []):
        if isinstance(child, dict):
            yield from _walk(child, f"{here}[{index}]")


def check_blocks(blocks: list[dict], where: str) -> list[Finding]:
    """Check one page's block tree. Returns findings; empty means conformant."""
    findings: list[Finding] = []

    if not blocks:
        return [Finding(where, "empty", "the page has no blocks at all")]

    for index, root in enumerate(blocks):
        element = root.get("element")
        if element in SKELETON:
            findings.append(Finding(
                f"{where}#{index}", "skeleton-root",
                f"the page roots on <{element}>, which is document skeleton, not content. "
                "Builder owns the document; an unstyled <html> root shrink-wraps in the "
                "editor canvas and the page renders as a narrow left-aligned column "
                "(BS-022)."
            ))
        elif element not in ROOT_ELEMENTS:
            findings.append(Finding(
                f"{where}#{index}", "unusual-root",
                f"the page roots on <{element}>. Builder's own template emits only <div>."
            ))

    roots = {id(root) for root in blocks}
    for root in blocks:
        for path, block in _walk(root):
            element = block.get("element")
            # A skeleton *root* is already reported above, and it is a different
            # problem: it is the one that makes the editor canvas shrink-wrap.
            # Reporting it twice would conflate the two and inflate the count.
            if element in SKELETON and id(block) not in roots:
                findings.append(Finding(
                    f"{where}:{path}", "skeleton-block",
                    f"<{element}> is document skeleton and must not be a block. Its "
                    "content belongs in head_html, page_title or the stylesheet."
                ))

            for bucket in STYLE_BUCKETS:
                styles = block.get(bucket)
                if not isinstance(styles, dict):
                    continue
                family = styles.get("fontFamily") or styles.get("font-family")
                if isinstance(family, str):
                    if "," in family:
                        findings.append(Finding(
                            f"{where}:{path}", "font-stack",
                            f"{bucket}.fontFamily is a CSS stack ({family!r}). Builder's "
                            "editor URL-encodes the whole value into a Google Fonts "
                            "request, so a stack 400s and the font silently never loads "
                            "in the editor (BS-022)."
                        ))
                    if "\\" in family:
                        findings.append(Finding(
                            f"{where}:{path}", "font-escape",
                            f"{bucket}.fontFamily contains a CSS escape ({family!r}). "
                            "The escape is not part of the family name and Builder will "
                            "send it verbatim to Google Fonts."
                        ))
    return findings


def check_payload_dir(directory: Path) -> Report:
    """Check every `Builder Page` payload under a directory."""
    report = Report()
    for path in sorted(directory.rglob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            report.findings.append(Finding(str(path), "unreadable", str(exc)))
            continue
        if not isinstance(record, dict) or record.get("doctype") != "Builder Page":
            continue

        blocks: Any = record.get("blocks")
        if isinstance(blocks, str):
            # Payloads serialise blocks as JSON text, the way Frappe stores them.
            try:
                blocks = json.loads(blocks or "[]")
            except json.JSONDecodeError as exc:
                report.findings.append(Finding(str(path), "unreadable", f"blocks: {exc}"))
                continue
        if not isinstance(blocks, list):
            report.findings.append(
                Finding(str(path), "unreadable", f"blocks is {type(blocks).__name__}, not a list")
            )
            continue

        report.pages_checked += 1
        report.blocks_checked += sum(1 for root in blocks for _ in _walk(root))
        report.findings += check_blocks(blocks, record.get("route") or path.name)
    return report


def report(result: Report) -> None:
    print("block conformance — would Builder have written this?")
    print(f"  pages: {result.pages_checked}   blocks: {result.blocks_checked}\n")

    if result.pages_checked == 0:
        print("NOTHING CHECKED — no Builder Page payloads here.\n"
              "Not a pass. `main()` exits 2 for this; a caller reading only this\n"
              "text would otherwise take silence for approval.")
        return

    if result.ok:
        print("Conformant. Every page roots on a container, carries no document\n"
              "skeleton, and holds a single font family per style bucket.")
        return

    by_rule: dict[str, list[Finding]] = {}
    for finding in result.findings:
        by_rule.setdefault(finding.rule, []).append(finding)

    for rule, findings in sorted(by_rule.items()):
        print(f"{rule} — {len(findings)}")
        for finding in findings[:5]:
            print(f"  {finding.where}: {finding.detail}")
        if len(findings) > 5:
            print(f"  ... and {len(findings) - 5} more")
        print()

    print("These pages may render perfectly and still be broken. The workflow\n"
          "depends on someone opening them in Builder afterwards.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="buildsmith conformance", description=__doc__)
    parser.add_argument("--dir", type=Path, required=True,
                        help="a directory of Builder Page payloads")
    args = parser.parse_args(argv)

    if not args.dir.is_dir():
        print(f"COULD NOT CHECK: {args.dir} is not a directory", file=sys.stderr)
        return EXIT_UNCHECKED

    result = check_payload_dir(args.dir)
    report(result)
    if result.pages_checked == 0:
        print("\nCOULD NOT CHECK: no Builder Page payloads found.", file=sys.stderr)
        return EXIT_UNCHECKED
    return EXIT_OK if result.ok else EXIT_PROBLEM


if __name__ == "__main__":
    sys.exit(main())
