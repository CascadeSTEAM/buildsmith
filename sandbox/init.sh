#!/usr/bin/env bash
# buildsmith sandbox/init.sh — build a disposable bench running the pinned Builder.
#
# Idempotent and resumable: each stage checks whether it already ran, so a
# re-invocation after a network failure picks up where it stopped rather than
# starting over. Building the bench takes a while on a cold cache.
#
# Usage:
#   bash sandbox/init.sh              build (or resume) the sandbox
#   bash sandbox/init.sh --status     report what exists, change nothing
#   bash sandbox/init.sh --destroy    tear it down, volumes and all
#
# Environment:
#   SANDBOX_ALLOW_LOOSE_PIN=1   permit a branch/tag instead of a commit SHA
set -euo pipefail

SANDBOX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SANDBOX_DIR"

# shellcheck disable=SC1091
set -a; . ./pins.env; set +a

COMPOSE="docker compose"
BENCH_DIR="/home/frappe/frappe-bench"

log()  { printf '\n==> %s\n' "$*"; }
die()  { echo "ERROR: $*" >&2; exit 1; }

# Run a command inside the bench container as the frappe user. -T because we
# are never interactive here and a TTY breaks exit-code propagation in CI.
inbench() { $COMPOSE exec -T bench bash -lc "$1"; }

case "${1:-}" in
    --destroy)
        log "Tearing down the sandbox and its volumes"
        $COMPOSE down -v
        echo "Gone. Re-run without arguments to rebuild."
        exit 0
        ;;
    --status)
        $COMPOSE ps
        inbench "test -d $BENCH_DIR" 2>/dev/null \
            && inbench "cd $BENCH_DIR && bench version 2>/dev/null | grep -E 'frappe|builder'" \
            || echo "(no bench built yet)"
        exit 0
        ;;
    "") ;;
    *)  die "usage: init.sh [--status|--destroy]" ;;
esac

# --- the pins must actually pin ----------------------------------------------
# A branch name is not a pin. See the long note in pins.env: every develop
# commit for the last thousand-odd commits reports the same version string, and
# a doctype we depend on was renamed inside that range. Refuse the ambiguity.
require_sha() {
    local name="$1" value="$2"
    [[ "$value" =~ ^[0-9a-f]{40}$ ]] && return 0
    if [ "${SANDBOX_ALLOW_LOOSE_PIN:-0}" = "1" ]; then
        echo "WARNING: $name='$value' is not a commit SHA."
        echo "         Results from this sandbox are not reproducible."
        return 0
    fi
    die "$name must be a 40-character commit SHA, got '$value'.
A branch or tag makes sandbox results unreproducible — Builder reports the same
version string across a thousand-plus develop commits, including a rename of a
doctype we depend on. Override with SANDBOX_ALLOW_LOOSE_PIN=1 if you mean it."
}
require_sha BUILDER_REF "$BUILDER_REF"
require_sha FRAPPE_REF "$FRAPPE_REF"

for pin in BUILDER FRAPPE; do
    eval "status=\${${pin}_REF_STATUS:-unset}"
    if [ "$status" != "confirmed" ]; then
        echo "NOTE: ${pin}_REF is '$status', not 'confirmed'."
        echo "      The pin is inferred, not read off the target site. Sandbox results"
        echo "      are reproducible but not yet known to match production."
    fi
done

# Pin an app checkout to a SHA. Fetch by URL rather than remote name: `bench
# get-app` clones with `--origin upstream`, and `bench init` names frappe's
# remote `upstream` too, so assuming `origin` fails. The clones are also
# --depth 1, so the pin is not in the shallow history and must be fetched.
# --force because the build rewrites tracked files (yarn.lock among them) and a
# dirty tree blocks checkout; nothing in these working copies is ours to keep.
pin_app() {
    local app="$1" repo="$2" ref="$3"
    inbench "cd $BENCH_DIR/apps/$app \
             && git fetch --depth 1 '$repo' '$ref' \
             && git checkout --detach --force FETCH_HEAD"
}

# Verify rather than assume. An early version of this script fetched from a
# remote that does not exist; it failed loudly, but had the fetch merely been a
# no-op the sandbox would have run a different commit while claiming the pin —
# the exact failure pinning exists to prevent.
verify_app() {
    local app="$1" want="$2" got
    got=$(inbench "cd $BENCH_DIR/apps/$app && git rev-parse HEAD" | tr -d '\r\n')
    [ "$got" = "$want" ] || die "$app is at $got, not the pin $want.
The sandbox would be testing something other than what it claims. Refusing to continue."
    echo "    $app verified at $got"
}

# --- containers --------------------------------------------------------------
log "Starting containers"
$COMPOSE up -d
$COMPOSE exec -T bench true || die "bench container did not come up"

# --- bench -------------------------------------------------------------------
if inbench "test -d $BENCH_DIR/apps/frappe"; then
    log "Bench already initialised — skipping"
else
    log "Initialising bench (frappe @ $FRAPPE_BRANCH) — this is the slow part"
    inbench "cd /home/frappe && bench init \
        --skip-redis-config-generation \
        --frappe-branch '$FRAPPE_BRANCH' \
        frappe-bench"
fi

# bench init clones the branch tip, which is not the pin. Move it, then realign
# the venv: a different framework commit can want different python deps.
FRAPPE_AT=$(inbench "cd $BENCH_DIR/apps/frappe && git rev-parse HEAD" | tr -d '\r\n')
if [ "$FRAPPE_AT" != "$FRAPPE_REF" ]; then
    log "Moving frappe from ${FRAPPE_AT:0:12} to ${FRAPPE_REF:0:12}"
    pin_app frappe "$FRAPPE_REPO" "$FRAPPE_REF"
    inbench "cd $BENCH_DIR && bench setup requirements --python"
    FRAPPE_MOVED=1
else
    log "frappe already at ${FRAPPE_REF:0:12}"
fi
verify_app frappe "$FRAPPE_REF"

log "Pointing bench at the container services"
inbench "cd $BENCH_DIR && \
    bench set-config -g db_host mariadb && \
    bench set-config -g redis_cache 'redis://redis-cache:6379' && \
    bench set-config -g redis_queue 'redis://redis-queue:6379' && \
    bench set-config -g redis_socketio 'redis://redis-queue:6379'"

# --- builder, at the pin -----------------------------------------------------
if inbench "test -d $BENCH_DIR/apps/builder"; then
    CURRENT=$(inbench "cd $BENCH_DIR/apps/builder && git rev-parse HEAD" | tr -d '\r\n')
    if [ "$CURRENT" != "$BUILDER_REF" ]; then
        log "Moving Builder from ${CURRENT:0:12} to ${BUILDER_REF:0:12}"
        pin_app builder "$BUILDER_REPO" "$BUILDER_REF"
        REBUILD_ASSETS=1
    else
        log "Builder already at ${BUILDER_REF:0:12}"
    fi
else
    log "Fetching Builder, then moving it to ${BUILDER_REF:0:12}"
    inbench "cd $BENCH_DIR && bench get-app --branch '$BUILDER_BRANCH' builder '$BUILDER_REPO'"
    pin_app builder "$BUILDER_REPO" "$BUILDER_REF"
    REBUILD_ASSETS=1
fi
verify_app builder "$BUILDER_REF"

# The app's code changed under the built assets, so they no longer match.
if [ "${REBUILD_ASSETS:-0}" = "1" ]; then
    log "Rebuilding Builder assets for the pin"
    inbench "cd $BENCH_DIR && bench build --app builder"
fi

# --- site --------------------------------------------------------------------
if inbench "test -d $BENCH_DIR/sites/$SITE_NAME"; then
    log "Site $SITE_NAME already exists — skipping creation"
else
    log "Creating site $SITE_NAME"
    # The flag for "the database is not on a local socket" was renamed between
    # Frappe versions; try the current one and fall back rather than pinning
    # ourselves to a framework version we do not otherwise care about.
    inbench "cd $BENCH_DIR && bench new-site '$SITE_NAME' \
        --db-root-password '$DB_ROOT_PASSWORD' \
        --admin-password '$ADMIN_PASSWORD' \
        --mariadb-user-host-login-scope='%'" \
    || inbench "cd $BENCH_DIR && bench new-site '$SITE_NAME' \
        --mariadb-root-password '$DB_ROOT_PASSWORD' \
        --admin-password '$ADMIN_PASSWORD' \
        --no-mariadb-socket"
fi

if inbench "cd $BENCH_DIR && bench --site '$SITE_NAME' list-apps 2>/dev/null | grep -qw builder"; then
    log "Builder already installed on $SITE_NAME"
else
    log "Installing Builder on $SITE_NAME"
    inbench "cd $BENCH_DIR && bench --site '$SITE_NAME' install-app builder"
fi

# Moving a pin moves the *code*; the database schema stays where it was. Frappe
# 16.27.1 running against a 16.25.0 schema fails on the first field the new code
# expects and the old table lacks — observed as
# `AttributeError: 'SystemSettings' object has no attribute 'enable_snapshot_reports'`.
# So a pin change is always followed by a migrate.
# Unconditionally, not "if the pin moved this run". A previous run can move the
# code and then fail before migrating, leaving the schema stale with nothing to
# notice it — which is exactly what happened. `migrate` is idempotent and cheap
# when there is nothing to do, so the simple version is also the correct one.
log "Migrating $SITE_NAME so the schema matches the pinned code"
inbench "cd $BENCH_DIR && bench --site '$SITE_NAME' migrate" 2>&1 | tail -2

# TRAP-006: is_template is developer-mode gated, and the gate is on the site.
# TRAP-008: a blank time_zone stores timestamps in IST. Settle both up front —
# in the sandbox they are free, and on a live site they are a procedure.
log "Applying sandbox site settings (TRAP-006, TRAP-008)"
inbench "cd $BENCH_DIR && bench --site '$SITE_NAME' set-config developer_mode 1"
# Both fields in one write: System Settings has `language` as mandatory, so
# setting time_zone alone fails validation on a fresh site.
inbench "cd $BENCH_DIR && bench --site '$SITE_NAME' execute frappe.client.set_value \
    --args \"['System Settings','System Settings',{'language':'en','time_zone':'UTC'}]\"" \
    >/dev/null   # set_value echoes the entire doc; we only care that it succeeded

log "Sandbox ready"
inbench "cd $BENCH_DIR && bench version 2>/dev/null | grep -E 'frappe|builder'" || true
echo
echo "  Builder pinned at: $BUILDER_REF ($BUILDER_REF_STATUS)"
echo "  Next: buildsmith check traps"
