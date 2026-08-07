#!/usr/bin/env python3
"""Cross-check `buildsmith simulate` against the real Builder in the sandbox.

`simulate.py` reproduces `extend_block()`'s shell matching from the outside. A
reproduction is a claim about somebody else's code, and claims rot: upstream
changes the loop, our copy keeps answering confidently, and the answer is
quietly wrong in the one direction that matters — telling you a payload is safe.

So this runs the same scenarios through both and fails on any disagreement. It
checks both directions deliberately: a simulator that always predicted "collapse"
would agree with Builder on every damaging case and be useless.

    buildsmith check simulate           # needs the sandbox running

Verified against the pinned Builder commit (`sandbox/pins.env`).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from buildsmith.tools import simulate as simulate_mod

ROOT = Path(__file__).resolve().parents[2]


def comp(child_id, *, styled=False, extra=()):
    child = {"blockId": child_id, "element": "nav", "innerHTML": "Home"}
    if styled:
        child["baseStyles"] = {"color": "var(--u1, #000)"}
    return {"blockId": "root", "element": "header", "children": [child, *extra]}


def shell(reference):
    return {
        "blockId": "s1",
        "referenceBlockId": "root",
        "extendedFromComponent": "site-header",
        "children": [
            {
                "blockId": "s2",
                "referenceBlockId": reference,
                "element": None,
                "innerHTML": None,
                "baseStyles": {},
                "children": [],
            }
        ],
    }


#: (label, current component, proposed component, page shell, expected collapse)
SCENARIOS = [
    ("ids preserved, restyled", comp("nav-1"), comp("nav-1", styled=True), shell("nav-1"), False),
    ("id re-issued", comp("nav-1"), comp("REISSUED"), shell("nav-1"), True),
    ("child removed", comp("nav-1"), {"blockId": "root", "element": "header", "children": []},
     shell("nav-1"), True),
    ("child added alongside", comp("nav-1"),
     comp("nav-1", extra=({"blockId": "added", "element": "button", "innerHTML": "NEW"},)),
     shell("nav-1"), False),
    ("shell already orphaned", comp("nav-1"), comp("nav-1"), shell("long-gone"), False),
]

def main(argv: list[str] | None = None) -> int:
    # --- our prediction ---------------------------------------------------------
    predictions = []
    for _label, current, proposed, page_shell, _ in SCENARIOS:
        state = {
            "components": {"site-header": {"block": current}},
            "pages": [{"name": "home", "route": "/", "blocks": [page_shell]}],
        }
        report = simulate_mod.simulate(state, [{"component_id": "site-header", "block": proposed}])
        predictions.append(bool(report.collapses))

    # --- what Builder actually does ---------------------------------------------
    # Builder must answer the same question simulate.py answers: not "is this node
    # element=None" but "did this payload make it so". A shell that was already
    # orphaned renders as element=None under both trees, and blaming the payload for
    # it would be wrong — so render both and compare, exactly as simulate.py does.
    BUILDER_SIDE = (Path(__file__).parent / "bench_scripts" / "simulate_check.py").read_text()

    payload = [[current, proposed, page_shell] for _, current, proposed, page_shell, _ in SCENARIOS]
    scenarios_path = Path("/tmp/simulate-scenarios.json")
    scenarios_path.write_text(json.dumps(payload))

    compose = ["docker", "compose", "-f", str(ROOT / "sandbox" / "docker-compose.yml")]
    subprocess.run(
        [*compose, "cp", str(scenarios_path), "bench:/simulate-scenarios.json"],
        check=True, capture_output=True,
    )
    completed = subprocess.run(
        [*compose, "exec", "-T", "bench", "bash", "-lc",
         "cd /home/frappe/frappe-bench/sites && /home/frappe/frappe-bench/env/bin/python -"],
        input=BUILDER_SIDE, text=True, capture_output=True,
    )
    if completed.returncode != 0:
        print(completed.stdout)
        print(completed.stderr, file=sys.stderr)
        raise SystemExit("the Builder side did not run — is the sandbox up?")

    line = next(ln for ln in completed.stdout.splitlines() if ln.startswith("RESULT:"))
    actual = json.loads(line[len("RESULT:"):])

    # --- compare ----------------------------------------------------------------
    print("simulate.py vs the pinned Builder\n")
    failed = 0
    for (label, _, _, _, expected), predicted, real in zip(
            SCENARIOS, predictions, actual, strict=True):
        agree = predicted == real
        # Disagreeing with our own expectation is a finding too: it means the
        # scenario no longer means what it was written to mean.
        as_expected = real == expected
        status = "PASS" if agree and as_expected else "FAIL"
        if status == "FAIL":
            failed += 1
        print(f"  {status}  {label:28} predicted={predicted!s:5} builder={real!s:5}")
        if not agree:
            print("        simulate.py and Builder disagree — the reproduction is wrong")
        elif not as_expected:
            print(f"        both say {real}, but this scenario expected {expected} — "
                  "Builder's behaviour changed")

    print()
    if failed:
        print(f"SIMULATION IS NOT FAITHFUL — {failed} of {len(SCENARIOS)} scenarios.")
        print("buildsmith simulate no longer models the pinned Builder. Anything it calls safe")
        print("is now an unverified claim; fix it before trusting another dry run.")
        return 1

    print(f"simulate.py matches the pinned Builder — {len(SCENARIOS)}/{len(SCENARIOS)} scenarios.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
