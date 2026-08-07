#!/usr/bin/env python3
"""Bootstrap a Buildsmith working copy.

Run with the system Python, from a fresh clone, with nothing installed:

    python3 install.py            # what a designer or content editor needs
    python3 install.py --dev      # everything above, plus the developer toolchain

There are two audiences and they need different things (ADR-006):

  Designers and content editors run Buildsmith out of a container. The only
  thing that has to exist on their machine is a container runtime, so that is
  all this checks for and all the wrapper needs.

  Developers run it from the clone, the way it is developed. That needs a
  virtualenv, an editable install, the browser used by `buildsmith verify`, and
  the publication guard wired into git.

Deliberately stdlib-only and single-file: this is what runs *before* anything is
installed, so it cannot import from the package it is about to install, and it
must work on a Python that has no pip packages at all.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"

EXIT_OK = 0
EXIT_PROBLEM = 1

MIN_PYTHON = (3, 11)


def say(message: str) -> None:
    print(f"==> {message}")


def warn(message: str) -> None:
    print(f"WARNING: {message}", file=sys.stderr)


def fail(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return EXIT_PROBLEM


def run(*cmd: str, **kwargs) -> int:
    """Run a command, echoing it first so a failed bootstrap is reproducible."""
    print(f"    $ {' '.join(cmd)}")
    return subprocess.run(cmd, check=False, **kwargs).returncode


# --- what both audiences need ----------------------------------------------


def container_runtime() -> str | None:
    """Docker or Podman, whichever is present and actually usable.

    Presence on PATH is not enough — a docker client with no reachable daemon is
    the single most common way this fails, and it fails much later and much less
    clearly if we only check for the binary.
    """
    for name in ("docker", "podman"):
        if shutil.which(name) is None:
            continue
        probe = subprocess.run(
            [name, "info"], capture_output=True, text=True, check=False
        )
        if probe.returncode == 0:
            return name
        warn(f"{name} is installed but not responding ({name} info failed).")
    return None


def check_runtime() -> bool:
    runtime = container_runtime()
    if runtime is None:
        warn(
            "no working container runtime found (looked for docker and podman).\n"
            "         The local Builder sandbox cannot start without one. Install\n"
            "         Docker or Podman, then re-run this script."
        )
        return False
    say(f"container runtime: {runtime}")
    return True


# --- the developer path -----------------------------------------------------


def venv_python() -> Path:
    bindir = "Scripts" if os.name == "nt" else "bin"
    return VENV / bindir / ("python.exe" if os.name == "nt" else "python")


def ensure_venv() -> int:
    if venv_python().exists():
        say(f"virtualenv already present at {VENV}")
        return EXIT_OK
    say(f"creating a virtualenv at {VENV}")
    venv.EnvBuilder(with_pip=True, upgrade_deps=True).create(VENV)
    return EXIT_OK


def install_package(extras: str) -> int:
    py = str(venv_python())
    say(f"installing buildsmith[{extras}] in editable mode")
    return run(py, "-m", "pip", "install", "--quiet", "--editable", f".[{extras}]", cwd=ROOT)


def install_browser() -> int:
    """Chromium for `buildsmith verify`'s browser check.

    Not fatal if it fails. `buildsmith verify` reports exit code 2 ("could not
    check") rather than passing when the browser is absent, so a partial install
    degrades to a check that refuses to lie — which is the behaviour we want
    from a bootstrap script that hit a network problem.
    """
    py = str(venv_python())
    say("installing the Chromium build used by the browser check")
    code = run(py, "-m", "playwright", "install", "chromium", cwd=ROOT)
    if code != EXIT_OK:
        warn(
            "playwright could not install Chromium.\n"
            "         `buildsmith verify` will exit 2 (could not check) until it can.\n"
            "         Re-run: .venv/bin/python -m playwright install chromium"
        )
    return EXIT_OK


def check_gitleaks() -> int:
    """Report whether the generic secret scanner is present (ADR-010).

    Not fatal here, for the same reason as the browser: the hooks fail closed
    (exit 2, "could not check") when gitleaks is absent, so a missing binary
    blocks commits rather than letting them through unscanned. This check just
    tells the developer *now*, while they are already installing things,
    instead of at their first commit.
    """
    if shutil.which("gitleaks"):
        say("gitleaks found — generic secret scanning is active")
        return EXIT_OK
    warn(
        "gitleaks is not installed. Commits will be refused (exit 2) until it is.\n"
        "         One static binary: https://github.com/gitleaks/gitleaks/releases"
    )
    return EXIT_OK


def install_hooks() -> int:
    """Wire the publication guard into git.

    Per-clone by necessity: git's core.hooksPath is local config, so a fresh
    clone has the guard switched off until something turns it on. That is
    exactly the state in which a client-identifying file gets committed, which
    is why this is part of the bootstrap rather than a step to remember.
    """
    say("installing the publication guard as a git hook")
    return run(str(venv_python()), "-m", "buildsmith.tools.hooks", cwd=ROOT)


def developer_bootstrap() -> int:
    if sys.version_info < MIN_PYTHON:
        return fail(
            f"Python {'.'.join(map(str, MIN_PYTHON))}+ is required; "
            f"this is {'.'.join(map(str, sys.version_info[:3]))}."
        )
    if not (ROOT / ".git").exists():
        warn("this is not a git clone — the publication guard cannot be installed.")

    code = ensure_venv()
    if code != EXIT_OK:
        return code
    code = install_package("dev")
    if code != EXIT_OK:
        return fail("the editable install failed; nothing further was attempted.")

    install_browser()
    check_gitleaks()
    if (ROOT / ".git").exists():
        install_hooks()

    print(
        f"""
Developer setup complete.

    source {VENV.name}/bin/activate
    buildsmith --help
    buildsmith test               # prove the install, including the guard
    buildsmith sandbox up         # the pinned local Builder instance
"""
    )
    return EXIT_OK


def user_bootstrap() -> int:
    ok = check_runtime()
    print(
        """
Buildsmith runs out of a container for design and content work: the runtime
above is the only thing it needs from this machine.

The container image and its wrapper command are not built yet (ROADMAP M4).
Until they are, use the developer path:

    python3 install.py --dev
"""
    )
    return EXIT_OK if ok else EXIT_PROBLEM


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="install.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="install the developer toolchain: virtualenv, editable install, "
        "browser, git hooks",
    )
    args = parser.parse_args(argv)

    if args.dev:
        # Checked but never fatal here: a developer can write and test the
        # emitters all day without a container. Only the sandbox needs one.
        check_runtime()
        return developer_bootstrap()
    return user_bootstrap()


if __name__ == "__main__":
    sys.exit(main())
