"""`buildsmith` — one entry point for the whole workflow.

This is *the* interface. Make is a developer convenience that calls it, the
container's entrypoint is this, and a TUI will sit on the same library
underneath. Keeping one implementation means the three cannot drift, which is
the failure mode a Makefile-as-interface guarantees.

Deliberately `argparse` and nothing else. `buildsmith.primitives` and
`buildsmith.workflows` have no runtime dependencies and this should not be the
thing that introduces one — a designer running the container and a developer
running from a clone should be executing identical code.

    buildsmith clone --site acme --source https://acme.test/
    buildsmith verify --site acme --source https://acme.test/
    buildsmith capture --site acme
    buildsmith drift --site acme --source https://acme.test/
    buildsmith publish-verify --site acme
    buildsmith handoff --site acme

Every command returns a meaningful exit code: 0 proved, 1 found a problem, 2
could not check. The third is separate on purpose — "I could not verify this"
must never read as "this is fine".
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from buildsmith import __version__
from buildsmith.errors import (
    EXIT_OK,
    EXIT_PROBLEM,
    EXIT_UNCHECKED,
    CouldNotCheck,
)


#: Where sites live. Overridable so the container can mount a working directory
#: rather than assuming the repo layout.
def project_root() -> Path:
    import os

    return Path(os.environ.get("BUILDSMITH_ROOT", Path(__file__).resolve().parent.parent))



# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_clone(args: argparse.Namespace) -> int:
    """Crawl a source site, convert it, and load it into the dev instance."""

    from buildsmith.workflows.replicate import (
        crawl_site,
        emit,
        extract_site,
        fetch_assets,
        replicate,
        save_crawl,
    )

    site_dir = project_root() / "sites" / args.site
    crawl_dir = site_dir / "crawl"
    crawl_dir.mkdir(parents=True, exist_ok=True)

    crawl = crawl_site(args.source, max_pages=args.max_pages,
                       ignore_robots=args.ignore_robots, render=args.render)
    save_crawl(crawl, crawl_dir)
    fetch_assets(crawl, site_dir / "assets")
    print(crawl.summary())

    inventory = extract_site(crawl_dir, site=args.site)
    inventory.write(site_dir / "features.json")
    print()
    print(inventory.summary())

    # assets_dir is where fetch_assets just saved the linked stylesheets —
    # without it, every sheet is fetched and then never found, and the clone
    # converts with no appearance (the exact miss this option exists for).
    result = replicate(crawl_dir, site=args.site,
                       project_folder=args.project_folder,
                       assets_dir=site_dir / "assets")
    emit(result, site_dir / "build")
    print()
    print(result.summary())

    if result.coverage < 1.0:
        print("\nNot every route converted — fix that before loading it.", file=sys.stderr)
        return EXIT_PROBLEM

    if not args.no_load:
        from buildsmith.tools import load_dev

        load_dev.load(args.site, target=args.target)
    return EXIT_OK


def cmd_load(args: argparse.Namespace) -> int:
    """Load an already-emitted `build/` payload into the dev instance.

    The other half of `clone --no-load` (#13): crawling, converting, and
    emitting `sites/<site>/build/` already happen unconditionally before
    that flag's gate, so a deferred load needs none of it redone — only
    `load_dev.load` itself, which this wraps directly.
    """
    from buildsmith.tools import load_dev

    load_dev.load(args.site, with_assets=not args.no_assets, target=args.target)
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    """Content diff plus a browser check. Both, because they prove different things."""
    from buildsmith.tools import clone_diff, conformance

    problems = 0
    unchecked = 0

    # First, and without a browser: is this even a document Builder could have
    # written? The content diff and the browser check both compare RENDERED
    # output, so both once passed a clone whose root block was <html> (BS-023).
    # `conformance.main()` guards the zero-payload case with exit 2; calling
    # the library directly used to lose that guard, so a build dir with no
    # page payloads read as conformant here.
    build_dir = project_root() / "sites" / args.site / "build"
    if build_dir.is_dir():
        shape = conformance.check_payload_dir(build_dir)
        conformance.report(shape)
        print()
        if shape.pages_checked == 0:
            unchecked += 1
        else:
            problems += not shape.ok
    else:
        print(f"no build at {build_dir} — conformance not checked\n")
        unchecked += 1

    if args.source:
        result = clone_diff.compare(args.source, args.clone)
        clone_diff.report(result, args.source, args.clone)
        problems += not result.ok
    else:
        print("no --source given, skipping the content diff\n")

    # playwright is imported lazily *inside* check_site, so guarding only the
    # module import lets the failure escape as a traceback at call time — which
    # is neither "passed" nor "could not check", it is just noise. Guard the
    # call, and be explicit that an unchecked verify is not a clean one.
    try:
        from buildsmith.tools import visual_check

        visual = visual_check.check_site(args.site, args.clone)
    except CouldNotCheck as exc:
        # Caught HERE, not left to main()'s handler: a problem the earlier
        # legs already found must keep exit 1 — "could not check the rest"
        # never outranks "found a problem".
        print(f"COULD NOT run the browser check: {exc}", file=sys.stderr)
        return EXIT_PROBLEM if problems else EXIT_UNCHECKED
    except ModuleNotFoundError as exc:
        if "playwright" not in str(exc):
            raise
        print(
            "\nCOULD NOT run the browser check — playwright is not installed here.\n"
            "  Install:  pip install '.[visual]' && playwright install chromium\n"
            "  Or run:   .venv/bin/python -m buildsmith.cli verify ...\n"
            "\nThis verify checked content only. Nothing has confirmed that any\n"
            "feature actually works — a handler that binds and does nothing looks\n"
            "identical to one that works in a content diff.",
            file=sys.stderr,
        )
        return EXIT_PROBLEM if problems else EXIT_UNCHECKED
    print(f"\nvisual-check: {len(visual.passed)} passed, {len(visual.failed)} failed, "
          f"{len(visual.skipped)} inconclusive")
    for line in visual.skipped:
        print(f"  ????  {line}")
    for line in visual.failed:
        print(f"  FAIL  {line}")
    problems += not visual.ok
    if problems:
        return EXIT_PROBLEM
    # A verify with an unchecked leg is not a clean verify: "could not check"
    # must never read as "this is fine".
    return EXIT_UNCHECKED if unchecked else EXIT_OK


def cmd_capture(args: argparse.Namespace) -> int:
    from buildsmith.tools import capture_dev

    manifest = capture_dev.capture(
        args.site, target=args.target, transport=args.transport
    )
    print(f"captured {manifest['counts']} from {manifest['captured_from']}"
          f" over {manifest['transport']}")
    print(f"content hash: {manifest['content_hash']}")
    return EXIT_OK


def cmd_drift(args: argparse.Namespace) -> int:
    from buildsmith.tools import drift as drift_tool

    result = drift_tool.check(args.site, args.source)
    drift_tool.report(result, args.source, args.site)
    if result.unreachable:
        return EXIT_UNCHECKED
    return EXIT_OK if result.clean else EXIT_PROBLEM


def cmd_publish_verify(args: argparse.Namespace) -> int:
    from buildsmith.tools import publish_verify

    return publish_verify.rehearse(args.site)


def cmd_build(args: argparse.Namespace) -> int:
    from buildsmith.workflows.theme import build_site

    result = build_site(project_root() / "sites" / args.site, site=args.site)
    print(f"counts: {result.counts}")
    for warning in result.warnings:
        print(f"  WARN: {warning}")
    written = result.write(project_root() / "sites" / args.site / "build")
    print(f"wrote {len(written)} payload(s)")
    return EXIT_OK


def cmd_validate(args: argparse.Namespace) -> int:
    from buildsmith.tools import validate

    directory = args.dir or str(project_root() / "sites" / args.site / "build")
    return validate.main(["--dir", directory])


def cmd_simulate(args: argparse.Namespace) -> int:
    from buildsmith.tools import simulate

    return simulate.main(
        ["--state", args.state] + [x for p in args.payload for x in ("--payload", p)]
    )


def cmd_audit(args: argparse.Namespace) -> int:
    from buildsmith.tools import audit

    return audit.main(["--scope", args.scope])


def cmd_docs(args: argparse.Namespace) -> int:
    from buildsmith.tools import docgen

    return docgen.main(["--check"] if args.check else [])


def cmd_handoff(args: argparse.Namespace) -> int:
    from buildsmith.tools import handoff

    return handoff.brief(project_root() / "sites" / args.site / "build")


def cmd_guard(args: argparse.Namespace) -> int:
    from buildsmith.tools import guard

    return guard.main(args.guard_args or ["--cached"])


def cmd_secretscan(args: argparse.Namespace) -> int:
    from buildsmith.tools import secretscan

    return secretscan.main(["--history"] if args.history else [])


def cmd_conformance(args: argparse.Namespace) -> int:
    from buildsmith.tools import conformance

    return conformance.main(["--dir", args.dir])


def cmd_test(args: argparse.Namespace) -> int:
    """Run the test suite: unit tests, then the generated-docs checks."""
    import unittest

    # Top level is the repo root, not tests/, so `tests.fixtures` resolves the
    # same way here as it does under `python3 -m unittest tests.test_audit`.
    root = project_root()
    sys.path.insert(0, str(root))
    loader = unittest.TestLoader()
    suite = loader.discover(str(root / "tests"), pattern="test_*.py",
                            top_level_dir=str(root))
    runner = unittest.TextTestRunner(verbosity=2 if args.verbose else 1)
    result = runner.run(suite)
    if not result.wasSuccessful():
        return EXIT_PROBLEM

    from buildsmith.tools import docgen, schemagen

    return max(docgen.main(["--check"]), schemagen.main(["--check"]))


def cmd_sandbox(args: argparse.Namespace) -> int:
    """Bring the local dev instance up, inspect it, serve it, or destroy it."""
    from buildsmith.tools import sandbox

    argv = [args.action]
    if args.allow_loose_pin:
        argv.append("--allow-loose-pin")
    if args.quiet:
        argv.append("--quiet")
    return sandbox.main(argv)


def cmd_adopt(args: argparse.Namespace) -> int:
    """Load a site's live export into the sandbox exactly (ADR-008 Maintain)."""
    from buildsmith.tools import adopt

    argv = ["--site", args.site, "--target", args.target]
    if args.prune:
        argv.append("--prune")
    if args.templates:
        argv.append("--templates")
    return adopt.main(argv)


def _prove_by_oracle(site: str) -> int:
    """Run the rendering oracle and record the verdict in the gate ledger.

    Every applied transform ends here (ADR-009: the oracle gates each one).
    A CannotCheck escape leaves the ledger entry pending and exits 2 via
    main()'s handler — unproved must never read as proved.
    """
    from buildsmith.workflows.optimize import gates
    from buildsmith.workflows.optimize import oracle as oracle_mod

    report = oracle_mod.run_oracle(site)
    gates.record_oracle(site, report["ok"], report.get("failed", 0))
    print(oracle_mod.render_report(report))
    return EXIT_OK if report["ok"] else EXIT_PROBLEM


def cmd_optimize(args: argparse.Namespace) -> int:
    """W3 Optimize (ADR-009): builderize a site already loaded in the sandbox.

    `baseline` captures the reference every transform answers to; `oracle`
    proves the site as served now still renders identically to that baseline.
    """
    from buildsmith.tools import journal
    from buildsmith.workflows.optimize import gates

    if args.step == "status":
        import json

        from buildsmith.workflows.optimize import status as status_mod

        data = status_mod.gather(args.site)
        if args.json:
            print(json.dumps(data, indent=2))
        else:
            print(status_mod.render(data))
        # Read-only view; pending gates are its headline, not its exit code —
        # a status that exits 1 gets wrapped in `|| true` and stops being read.
        return EXIT_OK

    if args.step == "baseline":
        from buildsmith.workflows.optimize import baseline as baseline_mod
        from buildsmith.workflows.optimize.shots import PlaywrightMissing

        try:
            manifest = baseline_mod.build_baseline(
                args.site, clone_url=args.clone or "http://127.0.0.1:8000",
                target=args.target, force=args.force)
        except PlaywrightMissing as exc:
            print(f"COULD NOT CHECK: {exc}\n"
                  "playwright is required for baseline screenshots: "
                  "python3 install.py --dev", file=sys.stderr)
            return EXIT_UNCHECKED
        except baseline_mod.CannotCapture as exc:
            print(f"COULD NOT CHECK: {exc}", file=sys.stderr)
            return EXIT_UNCHECKED
        route_summary = (f"baseline captured: {len(manifest['routes_captured'])} routes"
                        f" x {len(manifest['viewports'])} viewports")
        scripts_scanned = manifest["scripts_scanned"]
        if baseline_mod.scripts_unscanned(scripts_scanned):
            # "UNSCANNED — ..." already reads as a complete sentence;
            # appending "scripts scanned" produced two fragments jammed
            # together with no punctuation between them (#17).
            print(f"{route_summary}, {scripts_scanned}")
        else:
            print(f"{route_summary}, {scripts_scanned} scripts scanned")
        for route, status in manifest["routes_skipped"].items():
            print(f"  skipped {route!r}: HTTP {status} (unpublished?)")
        journal.append(args.site, "optimize baseline",
                       notes=f"{len(manifest['routes_captured'])} routes; "
                             f"checkpoint {manifest['checkpoint']['content_hash'][:12]}")
        return EXIT_OK

    if args.step == "tokenize":
        from buildsmith.workflows.optimize import tokenize as tokenize_mod

        routes = ([r.strip() for r in args.routes.split(",") if r.strip()]
                  if args.routes else None)
        if not args.apply:
            proposals = tokenize_mod.mine(args.site, routes=routes)
            path = tokenize_mod.proposal_path(args.site)
            print(f"{len(proposals['proposals'])} colour(s) mined -> {path}")
            print("name them, flip status to \"accepted\", then re-run "
                  "with --apply")
            return EXIT_OK
        result = tokenize_mod.apply(
            args.site, clone_url=args.clone or "http://127.0.0.1:8000",
            target=args.target, routes=routes)
        print(f"tokens: {len(result['tokens'])}  replacements: "
              f"{result['replacements']}  targets: {len(result['targets'])}")
        if result["unresolved"]:
            print("PROBLEM: unresolved token uuid(s) in the served "
                  f"stylesheet: {result['unresolved']}", file=sys.stderr)
        journal.append(args.site, "optimize tokenize",
                       notes=f"{result['replacements']} literals -> "
                             f"{len(result['tokens'])} tokens; "
                             + ("resolved" if result["ok"] else "UNRESOLVED"))
        if not result["ok"]:
            return EXIT_PROBLEM
        print("resolution proved: every token uuid is served. "
              "Running the rendering oracle —")
        return _prove_by_oracle(args.site)

    if args.step == "fonts":
        from buildsmith.workflows.optimize import fonts as fonts_mod

        routes = ([r.strip() for r in args.routes.split(",") if r.strip()]
                  if args.routes else None)
        if not args.apply:
            data = fonts_mod.mine(args.site, routes=routes)
            stacks = [p for p in data["proposals"] if p["status"] != "single"]
            print(f"{len(data['proposals'])} stack(s) mined "
                  f"({len(stacks)} reducible) -> "
                  f"{fonts_mod.proposal_path(args.site)}")
            print("review `primary`, flip status to \"accepted\" "
                  "(THIS ONE IS A VISIBLE CHANGE — sign it off), then --apply")
            return EXIT_OK
        result = fonts_mod.apply(
            args.site, clone_url=args.clone or "http://127.0.0.1:8000",
            target=args.target, routes=routes)
        print(f"reductions: {len(result['reductions'])}  replacements: "
              f"{result['replacements']}  targets: {len(result['targets'])}")
        if result["unmatched"]:
            print(f"NOTE: accepted but matched nothing (already applied?): "
                  f"{result['unmatched']}")
        if result["unloaded"]:
            print("PROBLEM: family not provably loaded on: "
                  f"{result['unloaded']}", file=sys.stderr)
        journal.append(args.site, "optimize fonts",
                       notes=f"{result['replacements']} stacks reduced; "
                             + ("loads proved" if result["ok"]
                                else "UNLOADED families"))
        if not result["ok"]:
            return EXIT_PROBLEM
        if result["coverage"] != "complete":
            print("COULD NOT FULLY CHECK: rewritten but unservable, so "
                  f"unproved: {result['unproved']}", file=sys.stderr)
            return EXIT_UNCHECKED
        print("loads proved on every rewritten route. Running the rendering "
              "oracle — the human sign-off on the proposal still applies.")
        return _prove_by_oracle(args.site)

    if args.step == "componentize":
        from buildsmith.workflows.optimize import componentize as comp_mod

        routes = ([r.strip() for r in args.routes.split(",") if r.strip()]
                  if args.routes else None)
        if args.apply:
            if routes:
                # unlike tokenize/fonts/collapse, one proposal spans however
                # many pages it was mined from — there is no honest partial
                # meaning for "apply only within these routes", so this
                # refuses rather than silently ignoring the flag.
                raise SystemExit(
                    "--routes is not supported with componentize --apply: "
                    "an accepted proposal applies to every instance it "
                    "lists, or is skipped whole — never partially.")
            result = comp_mod.apply(
                args.site, clone_url=args.clone or "http://127.0.0.1:8000",
                target=args.target)
            print(f"applied: {len(result['applied'])}  "
                  f"pages rewritten: {len(result['targets'])}")
            for skip in result["skipped"]:
                print(f"  NOT applied — {skip['name'] or skip['shape']}: "
                      f"{skip['reason']}", file=sys.stderr)
            journal.append(
                args.site, "optimize componentize",
                notes=f"{len(result['applied'])} component(s) extracted; "
                      f"{len(result['skipped'])} shape(s) skipped")
            if not result["applied"]:
                return EXIT_UNCHECKED
            print("extraction written. Running the rendering oracle — the "
                  "human sign-off on the proposal still applies.")
            return _prove_by_oracle(args.site)
        data = comp_mod.mine(args.site, routes=routes)
        print(f"{len(data['proposals'])} repeated structure(s) -> "
              f"{comp_mod.proposal_path(args.site)}")
        for p in data["proposals"][:8]:
            nested = (f"  (+{p['nested_pruned']} nested in bigger shapes)"
                      if p.get("nested_pruned") else "")
            print(f"  {p['occurrences']:>3}x {p['blocks_per_instance']:>4} "
                  f"blocks  <{p['element']}>  = {p['total_blocks']} total  "
                  f"[{p['status']}]{nested}")
        if data["orphaned"]:
            print(f"WARNING: {len(data['orphaned'])} past decision(s) whose "
                  "shape no longer exists moved to `orphaned` — review them.",
                  file=sys.stderr)
        return EXIT_OK

    if args.step == "collapse":
        from buildsmith.workflows.optimize import collapse as collapse_mod

        routes = ([r.strip() for r in args.routes.split(",") if r.strip()]
                  if args.routes else None)
        result = collapse_mod.run(args.site, target=args.target,
                                  routes=routes, apply=args.apply)
        verb = ("removed" if result["applied"] else
                "removable (nothing to apply)" if result["apply_requested"]
                else "removable (dry run)")
        print(f"{result['removed']} inert wrapper(s) {verb} across "
              f"{len(result['targets'])} page(s); log: {result['log_dir']}")
        for warning in result["warnings"]:
            print(f"WARNING: {warning}", file=sys.stderr)
        if result["components_skipped"]:
            print(f"NOTE: component tree(s) not collapsed (TRAP-001 mirror "
                  f"is unmanaged here): {result['components_skipped']}")
        if not result["applied"]:
            return EXIT_OK
        journal.append(args.site, "optimize collapse",
                       notes=f"{result['removed']} wrappers removed")
        # the structural rules are a filter; the oracle is the proof. An
        # applied collapse does not get to exit 0 on structure alone.
        return _prove_by_oracle(args.site)

    from buildsmith.workflows.optimize import oracle as oracle_mod

    kwargs = {}
    if args.threshold is not None:
        kwargs["threshold"] = args.threshold
    if args.tolerance is not None:
        kwargs["tolerance"] = args.tolerance
    report = oracle_mod.run_oracle(
        args.site, clone_url=args.clone or None, **kwargs)
    # A manual oracle run settles pending gate entries too — but ONLY at the
    # default strictness against the baseline's own URL. A loosened
    # --threshold 1 passes everything, and a gate cleared that way would be
    # indistinguishable from a real proof; the recorded-waiver path for
    # skipping the proof is `optimize baseline --force`, nothing else.
    if not kwargs and not args.clone:
        gates.record_oracle(args.site, report["ok"], report.get("failed", 0))
    else:
        print("NOTE: non-default oracle parameters — this run does not "
              "settle the gate ledger. Re-run with defaults, or waive with "
              "`optimize baseline --force` (which records the waiver).",
              file=sys.stderr)
    print(oracle_mod.render_report(report))
    journal.append(args.site, "optimize oracle",
                   notes=("unchanged" if report["ok"]
                          else f"{report['failed']} shot(s) differ"))
    return EXIT_OK if report["ok"] else EXIT_PROBLEM


def cmd_check(args: argparse.Namespace) -> int:
    """Prove the sandbox and the simulator still match the pinned Builder."""
    from buildsmith.tools import check_roundtrip, check_simulate, check_traps

    runners = {"traps": check_traps, "simulate": check_simulate,
               "roundtrip": check_roundtrip}
    names = list(runners) if args.what == "all" else [args.what]
    worst = 0
    for name in names:
        print(f"\n--- {name} ---")
        worst = max(worst, runners[name].main([]) or 0)
    return worst


def cmd_hooks(args: argparse.Namespace) -> int:
    """Install the git hooks. They are off in a fresh clone until this runs."""
    from buildsmith.tools import hooks

    return hooks.main(["--check"] if args.check else [])


def cmd_schema(args: argparse.Namespace) -> int:
    """Regenerate the Builder schema reference from the pinned sandbox."""
    from buildsmith.tools import schemagen

    return schemagen.main(["--check"] if args.check else [])


def cmd_golive(args: argparse.Namespace) -> int:
    """Generate the go-live plan. The tool table advertised this command
    while nothing wired it — a documented interface must parse."""
    from buildsmith.tools import golive

    argv = ["--site", args.site]
    if args.out:
        argv += ["--out", args.out]
    return golive.main(argv)


def cmd_journal(args: argparse.Namespace) -> int:
    from buildsmith.tools import journal

    if args.render:
        sys.stdout.write(journal.render(args.site))
        return EXIT_OK
    journal.append(args.site, args.tool or "manual", notes=args.note or "")
    print("journalled")
    return EXIT_OK


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="buildsmith",
        description="Design and maintain Frappe Builder websites.",
        epilog="Exit codes: 0 proved, 1 found a problem, 2 could not check.",
    )
    parser.add_argument("--version", action="version", version=f"buildsmith {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    def add(name: str, fn, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=fn.__doc__ or help_text)
        p.set_defaults(func=fn)
        return p

    p = add("clone", cmd_clone, "crawl a site, convert it, load it into dev")
    p.add_argument("--render", action="store_true",
                   help="crawl through a real browser — required for sites "
                        "that assemble themselves client-side (#5)")
    p.add_argument("--site", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--project-folder", default="buildsmith")
    p.add_argument("--max-pages", type=int, default=200)
    p.add_argument("--ignore-robots", action="store_true",
                   help="only for a site you control; make it a decision, not a default")
    p.add_argument("--no-load", action="store_true", help="emit payloads without loading")
    p.add_argument("--target", default="sandbox.localhost")

    p = add("load", cmd_load, "load an already-emitted build/ payload into dev")
    p.add_argument("--site", required=True)
    p.add_argument("--target", default="sandbox.localhost")
    p.add_argument("--no-assets", action="store_true")

    p = add("verify", cmd_verify, "content diff + browser check of the dev copy")
    p.add_argument("--site", required=True)
    p.add_argument("--source", default="", help="omit to skip the content diff")
    p.add_argument("--clone", default="http://127.0.0.1:8000")

    p = add("capture", cmd_capture, "read the dev instance back into the private layer")
    p.add_argument("--site", required=True)
    p.add_argument("--target", default="sandbox.localhost")
    p.add_argument("--transport", choices=["auto", "rest", "bench"], default="auto",
                   help="auto prefers REST when BUILDSMITH_FRAPPE_TOKEN is set")

    p = add("drift", cmd_drift, "has the live site changed since we cloned it?")
    p.add_argument("--site", required=True)
    p.add_argument("--source", required=True)

    p = add("publish-verify", cmd_publish_verify, "rehearse the publish on a scratch site")
    p.add_argument("--site", required=True)

    p = add("build", cmd_build, "build payloads from a site's design inputs")
    p.add_argument("--site", required=True)

    p = add("validate", cmd_validate, "validate emitted payloads")
    p.add_argument("--site", default="example")
    p.add_argument("--dir")

    p = add("simulate", cmd_simulate, "dry-run a component payload against a state export")
    p.add_argument("--state", required=True)
    p.add_argument("--payload", action="append", required=True)

    p = add("audit", cmd_audit, "whole-repo publication sweep")
    p.add_argument("--scope", choices=["tree", "history", "refs", "all"], default="all")

    p = add("docs", cmd_docs, "regenerate the component catalog")
    p.add_argument("--check", action="store_true")

    p = add("handoff", cmd_handoff, "validate and print the operations handoff brief")
    p.add_argument("--site", required=True)

    p = add("guard", cmd_guard, "publication guard over staged changes or a range")
    p.add_argument("guard_args", nargs=argparse.REMAINDER,
                   help="--cached (default) | <base>...<head> | --message-file <path>")

    p = add("secretscan", cmd_secretscan,
            "generic credential scan via gitleaks (ADR-010)")
    p.add_argument("--history", action="store_true",
                   help="scan every commit ever made, not just staged changes")

    p = add("conformance", cmd_conformance,
            "would Builder have written these payloads?")
    p.add_argument("--dir", required=True)

    p = add("test", cmd_test, "run the test suite and the generated-docs checks")
    p.add_argument("-v", "--verbose", action="store_true")

    p = add("sandbox", cmd_sandbox, "the local Builder dev instance")
    p.add_argument("action", choices=["up", "status", "serve", "destroy", "token"])
    p.add_argument("--allow-loose-pin", action="store_true")
    p.add_argument("--quiet", action="store_true",
                   help="token: print only the value, for $(...) capture")

    p = add("adopt", cmd_adopt,
            "load a live export into the sandbox exactly (ADR-008)")
    p.add_argument("--site", required=True)
    p.add_argument("--target", default="sandbox.localhost")
    p.add_argument("--prune", action="store_true")
    p.add_argument("--templates", action="store_true")

    p = add("optimize", cmd_optimize,
            "W3: builderize a site in the sandbox (ADR-009)")
    p.add_argument("step", choices=["status", "baseline", "oracle", "tokenize",
                                    "fonts", "collapse", "componentize"])
    p.add_argument("--site", required=True)
    p.add_argument("--json", action="store_true",
                   help="status: machine-readable output")
    p.add_argument("--apply", action="store_true",
                   help="tokenize/fonts/collapse/componentize: apply accepted "
                        "proposals (default: mine/propose only)")
    p.add_argument("--routes", default="",
                   help="comma-separated route filter for the transforms")
    p.add_argument("--force", action="store_true",
                   help="baseline: rebuild even though an applied transform "
                        "has no passing oracle (the waiver is recorded in "
                        "the gate ledger)")
    p.add_argument("--clone", default="",
                   help="dev URL; baseline defaults to http://127.0.0.1:8000, "
                        "oracle to the baseline's own URL")
    p.add_argument("--target", default="sandbox.localhost")
    p.add_argument("--threshold", type=float, default=None,
                   help="oracle: fraction of pixels allowed to differ per "
                        "shot (default 0.0001; a non-default run does not "
                        "settle the gate ledger)")
    p.add_argument("--tolerance", type=int, default=None,
                   help="oracle: per-channel delta treated as noise "
                        "(default 2; a non-default run does not settle the "
                        "gate ledger)")

    p = add("check", cmd_check, "prove the sandbox still matches the pinned Builder")
    p.add_argument("what", nargs="?", default="all",
                   choices=["all", "traps", "simulate", "roundtrip"])

    p = add("hooks", cmd_hooks, "install the git hooks")
    p.add_argument("--check", action="store_true")

    p = add("schema", cmd_schema, "regenerate the Builder schema reference")
    p.add_argument("--check", action="store_true")

    p = add("golive", cmd_golive, "generate the go-live plan from the actual build")
    p.add_argument("--site", required=True)
    p.add_argument("--out", help="write here instead of stdout")

    p = add("journal", cmd_journal, "append to or render the run journal")
    p.add_argument("--site", required=True)
    p.add_argument("--render", action="store_true")
    p.add_argument("--tool")
    p.add_argument("--note")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except CouldNotCheck as exc:
        # The check never ran. Exit 2 is its own answer: "could not verify"
        # must be distinguishable from "found a problem", or a caller reading
        # exit 1 goes hunting for a defect that does not exist.
        # ORDER MATTERS: CouldNotCheck subclasses SystemExit (so an escape
        # that misses this handler still refuses), which means this clause
        # must stay ABOVE the SystemExit one or every "could not check"
        # silently becomes "found a problem" again.
        print(f"COULD NOT CHECK: {exc}", file=sys.stderr)
        return EXIT_UNCHECKED
    except SystemExit as exc:
        # A tool refused. `SystemExit` carries either an int code or a message —
        # several tools raise the message form, and assuming int turned a clear
        # refusal into "invalid literal for int()".
        code = exc.code
        if code is None:
            return EXIT_OK
        if isinstance(code, int):
            return code
        print(code, file=sys.stderr)
        return EXIT_PROBLEM


if __name__ == "__main__":
    raise SystemExit(main())
