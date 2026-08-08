"""The disposable Builder dev instance: build it, inspect it, tear it down.

Ported from a shell script because the shell bought nothing here — every line
was already shelling out to `docker compose` — and cost the ability to be called
from the CLI, the tests, or a TUI without spawning a subshell.

Idempotent and resumable: each stage checks whether it already ran, so a
re-invocation after a network failure picks up rather than starting over.

The container carve-out (ADR-002): local disposable containers live in-repo,
because a publishable project must be runnable by someone who does not have the
operations project. Anything touching a server does not live here.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SANDBOX = ROOT / "sandbox"
COMPOSE = ["docker", "compose", "-f", str(SANDBOX / "docker-compose.yml")]
BENCH = "/home/frappe/frappe-bench"

__all__ = ["Pins", "destroy", "load_pins", "run_bench", "status", "token", "up"]


class SandboxError(RuntimeError):
    """The sandbox cannot be brought to the state that was asked for."""


class Pins(dict):
    """`pins.env`, parsed. A dict so callers can read any key they need."""

    @property
    def builder(self) -> str:
        return self["BUILDER_REF"]

    @property
    def frappe(self) -> str:
        return self["FRAPPE_REF"]


def load_pins(path: Path | None = None) -> Pins:
    pins = Pins()
    for line in (path or SANDBOX / "pins.env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        pins[key.strip()] = value.split("#")[0].strip()
    return pins


def _require_sha(name: str, value: str, *, allow_loose: bool) -> None:
    """A branch name is not a pin.

    Builder reports the same version string across a thousand-plus develop
    commits, including a rename of a doctype the token layer depends on. Two
    sandboxes on different commits can both honestly claim the same version.
    """
    if re.fullmatch(r"[0-9a-f]{40}", value):
        return
    if allow_loose:
        print(f"WARNING: {name}={value!r} is not a commit SHA — results are "
              "not reproducible.", file=sys.stderr)
        return
    raise SandboxError(
        f"{name} must be a 40-character commit SHA, got {value!r}.\n"
        "A branch or tag makes sandbox results unreproducible. Override with "
        "--allow-loose-pin if you mean it."
    )


def _compose(*args: str, check: bool = True, capture: bool = True):
    return subprocess.run([*COMPOSE, *args], check=check, capture_output=capture, text=True)


def run_bench(script: str, *, site: str | None = None) -> str:
    """Run python inside the bench container and return stdout.

    The one place the sandbox is driven from the outside. Kept here so the
    tools that need it do not each grow their own copy of the incantation.
    """
    completed = subprocess.run(
        [*COMPOSE, "exec", "-T", "bench", "bash", "-lc",
         f"cd {BENCH}/sites && {BENCH}/env/bin/python -"],
        input=script, text=True, capture_output=True,
    )
    if completed.returncode != 0:
        print(completed.stdout)
        print(completed.stderr, file=sys.stderr)
        raise SandboxError("the bench rejected that script")
    return completed.stdout


def is_running() -> bool:
    result = subprocess.run(
        [*COMPOSE, "ps", "--status", "running", "--services"], capture_output=True, text=True
    )
    return "bench" in result.stdout.split()


def require_running() -> None:
    if not is_running():
        raise SandboxError("the sandbox is not running. Start it with: buildsmith sandbox up")


def _sh(command: str, *, quiet: bool = False) -> str:
    """Run a shell command inside the bench container."""
    completed = subprocess.run(
        [*COMPOSE, "exec", "-T", "bench", "bash", "-lc", command],
        capture_output=True, text=True,
    )
    if completed.returncode != 0 and not quiet:
        print(completed.stdout)
        print(completed.stderr, file=sys.stderr)
        raise SandboxError(f"failed inside the container: {command[:70]}")
    return completed.stdout


def _pin_app(app: str, repo: str, ref: str) -> None:
    """Move an app to a commit.

    Fetch by URL, not remote name: `bench get-app` clones with `--origin
    upstream` and `bench init` does the same for frappe, so assuming `origin`
    fails. The clones are `--depth 1`, so the pin is not in the shallow history
    and must be fetched explicitly. `--force` because the build rewrites tracked
    files and a dirty tree blocks checkout — nothing in these working copies is
    ours to keep.
    """
    _sh(f"cd {BENCH}/apps/{app} && git fetch --depth 1 '{repo}' '{ref}' "
        f"&& git checkout --detach --force FETCH_HEAD")


def _verify_app(app: str, want: str) -> None:
    """Verify rather than assume.

    An early version of this fetched from a remote that did not exist. It failed
    loudly — but had the fetch merely been a no-op, the sandbox would have run a
    different commit while claiming the pin, which is the exact failure pinning
    exists to prevent.
    """
    got = _sh(f"cd {BENCH}/apps/{app} && git rev-parse HEAD").strip()
    if got != want:
        raise SandboxError(
            f"{app} is at {got}, not the pin {want}.\n"
            "The sandbox would be testing something other than what it claims."
        )
    print(f"    {app} verified at {got}")


def up(*, allow_loose: bool = False) -> int:
    pins = load_pins()
    _require_sha("BUILDER_REF", pins.builder, allow_loose=allow_loose)
    _require_sha("FRAPPE_REF", pins.frappe, allow_loose=allow_loose)

    for name in ("BUILDER", "FRAPPE"):
        if pins.get(f"{name}_REF_STATUS") != "confirmed":
            print(f"NOTE: {name}_REF is '{pins.get(f'{name}_REF_STATUS', 'unset')}', not "
                  "'confirmed' — inferred, not read off the target site.")

    site = pins["SITE_NAME"]
    print("\n==> Starting containers")
    _compose("up", "-d", capture=False)

    if _sh(f"test -d {BENCH}/apps/frappe && echo yes", quiet=True).strip() != "yes":
        print(f"\n==> Initialising bench (frappe @ {pins['FRAPPE_BRANCH']}) — the slow part")
        _sh(f"cd /home/frappe && bench init --skip-redis-config-generation "
            f"--frappe-branch '{pins['FRAPPE_BRANCH']}' frappe-bench")

    if _sh(f"cd {BENCH}/apps/frappe && git rev-parse HEAD").strip() != pins.frappe:
        print(f"\n==> Moving frappe to {pins.frappe[:12]}")
        _pin_app("frappe", pins["FRAPPE_REPO"], pins.frappe)
        _sh(f"cd {BENCH} && bench setup requirements --python")
    _verify_app("frappe", pins.frappe)

    print("\n==> Pointing bench at the container services")
    _sh(f"cd {BENCH} && bench set-config -g db_host mariadb "
        f"&& bench set-config -g redis_cache 'redis://redis-cache:6379' "
        f"&& bench set-config -g redis_queue 'redis://redis-queue:6379' "
        f"&& bench set-config -g redis_socketio 'redis://redis-queue:6379'")

    rebuilt = False
    if _sh(f"test -d {BENCH}/apps/builder && echo yes", quiet=True).strip() == "yes":
        if _sh(f"cd {BENCH}/apps/builder && git rev-parse HEAD").strip() != pins.builder:
            print(f"\n==> Moving Builder to {pins.builder[:12]}")
            _pin_app("builder", pins["BUILDER_REPO"], pins.builder)
            rebuilt = True
    else:
        print(f"\n==> Fetching Builder, then moving it to {pins.builder[:12]}")
        _sh(f"cd {BENCH} && bench get-app --branch '{pins['BUILDER_BRANCH']}' builder "
            f"'{pins['BUILDER_REPO']}'")
        _pin_app("builder", pins["BUILDER_REPO"], pins.builder)
        rebuilt = True
    _verify_app("builder", pins.builder)

    if rebuilt:
        print("\n==> Rebuilding Builder assets for the pin")
        _sh(f"cd {BENCH} && bench build --app builder")

    if _sh(f"test -d {BENCH}/sites/{site} && echo yes", quiet=True).strip() != "yes":
        print(f"\n==> Creating site {site}")
        try:
            _sh(f"cd {BENCH} && bench new-site '{site}' "
                f"--db-root-password '{pins['DB_ROOT_PASSWORD']}' "
                f"--admin-password '{pins['ADMIN_PASSWORD']}' "
                f"--mariadb-user-host-login-scope='%'")
        except SandboxError:
            _sh(f"cd {BENCH} && bench new-site '{site}' "
                f"--mariadb-root-password '{pins['DB_ROOT_PASSWORD']}' "
                f"--admin-password '{pins['ADMIN_PASSWORD']}' --no-mariadb-socket")

    if "builder" not in _sh(f"cd {BENCH} && bench --site '{site}' list-apps", quiet=True):
        print(f"\n==> Installing Builder on {site}")
        _sh(f"cd {BENCH} && bench --site '{site}' install-app builder")

    # Unconditionally, not "if the pin moved this run". A previous run can move
    # the code and then fail before migrating, leaving the schema stale with
    # nothing to notice it — which is exactly what happened once. `migrate` is
    # cheap when there is nothing to do, so the simple version is the correct one.
    print(f"\n==> Migrating {site} so the schema matches the pinned code")
    _sh(f"cd {BENCH} && bench --site '{site}' migrate")

    # TRAP-006: is_template is developer-mode gated, and the gate is on the site.
    # TRAP-008: a blank time_zone stores timestamps in IST. Both are free here
    # and a procedure on a live site.
    print("\n==> Applying sandbox site settings (TRAP-006, TRAP-008)")
    _sh(f"cd {BENCH} && bench --site '{site}' set-config developer_mode 1")
    _sh(f"cd {BENCH} && bench --site '{site}' execute frappe.client.set_value "
        f"--args \"['System Settings','System Settings',"
        f"{{'language':'en','time_zone':'UTC'}}]\" > /dev/null")

    print("\n==> Sandbox ready")
    print(_sh(f"cd {BENCH} && bench version 2>/dev/null | grep -E 'frappe|builder'").rstrip())
    print(f"\n  Builder pinned at: {pins.builder} ({pins.get('BUILDER_REF_STATUS')})")
    print(f"  Editor: http://127.0.0.1:8000/builder  (Administrator / "
          f"{pins['ADMIN_PASSWORD']})")
    print("  Next: buildsmith check traps")
    return 0


def status() -> int:
    _compose("ps", capture=False)
    if not is_running():
        return 0

    print(_sh(f"cd {BENCH} && bench version 2>/dev/null | grep -E 'frappe|builder'",
              quiet=True).rstrip() or "(no bench built yet)")

    # Printed every time, because the way in is not discoverable. A successful
    # login redirects to Website Settings.home_page, which for a cloned site is
    # the site's own home page — so signing in correctly looks exactly like
    # nothing happening, and the editor is somewhere you have to already know.
    pins = load_pins()
    print(f"""
Editor:   http://127.0.0.1:8000/builder
Desk:     http://127.0.0.1:8000/app
Sign in:  http://127.0.0.1:8000/login
          Administrator / {pins.get("ADMIN_PASSWORD", "admin")}

Log in FIRST, then go to /builder. Logging in drops you on the site's home
page, not the editor.""")
    return 0


MINT_TOKEN = """
import frappe
from frappe.utils.password import set_encrypted_password, remove_encrypted_password
frappe.init(site=%(site)r); frappe.connect()
key, secret = frappe.generate_hash(length=15), frappe.generate_hash(length=15)
frappe.db.set_value("User", "Administrator", "api_key", key)
# Rotate rather than reuse. A secret encrypted under a previous encryption key
# cannot be decrypted after a restore, and the failure surfaces as a confusing
# HTTP 401 "Failed to decrypt key" rather than as bad credentials.
try:
    remove_encrypted_password("User", "Administrator", "api_secret")
except Exception:
    pass
set_encrypted_password("User", "Administrator", secret, "api_secret")
frappe.db.commit()
print(f"{key}:{secret}")
"""


def token(quiet: bool = False) -> int:
    """Mint a fresh API token for the dev instance and print it.

    Rotating on every call is deliberate: this is a disposable local container,
    the token is worth nothing outside it, and a mint-if-missing branch would
    have to read the existing secret back — which fails after a site restore in
    a way that looks like an auth bug.

    The token is printed, never written to a file. Buildsmith reads credentials
    from the environment and does not store them (ADR-002, ADR-007).
    """
    require_running()
    pins = load_pins()
    site = pins.get("SITE_NAME", "sandbox.localhost")
    value = run_bench(MINT_TOKEN % {"site": site}).strip().splitlines()[-1]

    if quiet:
        print(value)
        return 0

    print(f"""A fresh API token for {site} (the previous one is now invalid):

    export BUILDSMITH_FRAPPE_TOKEN={value}

This is a throwaway credential for a disposable local container. It is printed
rather than saved — Buildsmith reads credentials from the environment and never
stores or resolves one.""")
    return 0


def destroy() -> int:
    print("Tearing down the sandbox and its volumes")
    _compose("down", "-v", capture=False)
    print("Gone. `buildsmith sandbox up` rebuilds it.")
    return 0


def serve(*, port: int = 8000) -> int:
    """Start the web server and the workers.

    Workers matter: `queue_action` locks documents and the lock outlives the
    request, so publishing anything with nothing draining the queue wedges it
    permanently (TRAP-009).
    """
    require_running()
    pins = load_pins()
    # Every result is checked: a failed `bench use` or a worker that never
    # spawned used to print "workers and scheduler up" regardless — and a
    # missing worker is TRAP-009's whole failure mode.
    used = subprocess.run([*COMPOSE, "exec", "-T", "bench", "bash", "-lc",
                           f"cd {BENCH} && bench use {pins['SITE_NAME']}"],
                          capture_output=True, text=True)
    if used.returncode != 0:
        raise SandboxError(
            f"bench use {pins['SITE_NAME']} failed:\n{used.stderr.strip()}"
        )
    for command in ("bench worker --queue short,default,long", "bench schedule",
                    f"bench serve --port {port}"):
        started = subprocess.run([*COMPOSE, "exec", "-d", "bench", "bash", "-lc",
                                  f"cd {BENCH} && exec {command}"],
                                 capture_output=True, text=True)
        if started.returncode != 0:
            raise SandboxError(
                f"could not start {command.split()[1]!r}:\n{started.stderr.strip()}"
            )
    # `exec -d` proves the processes launched, not that they stay up; the
    # worker gate in frappe_client still verifies a live worker before writes.
    print(f"serving on http://127.0.0.1:{port}/  (server, workers and scheduler started)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("action", choices=["up", "status", "destroy", "serve", "token"])
    parser.add_argument("--allow-loose-pin", action="store_true")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--quiet", action="store_true",
                        help="token: print only the value, for $(...) capture")
    args = parser.parse_args(argv)

    try:
        if args.action == "up":
            return up(allow_loose=args.allow_loose_pin)
        if args.action == "status":
            return status()
        if args.action == "serve":
            return serve(port=args.port)
        if args.action == "token":
            return token(quiet=args.quiet)
        return destroy()
    except SandboxError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
