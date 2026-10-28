#!/usr/bin/env bash
#
# fresh_instance.sh -- build this whole integration against a brand-new
#                      ServiceNow instance, in order, with checks.
#
#   ./fresh_instance.sh --check        # read-only against ServiceNow (writes .env locally)
#   ./fresh_instance.sh                # build: tables + verify + sample load + idempotency proof
#   ./fresh_instance.sh --full         # ...plus the complete feed
#   ./fresh_instance.sh --full --with-tests
#   ./fresh_instance.sh --help
#
# ─────────────────────────────────────────────────────────────────────────────
#  WHAT TO EDIT
# ─────────────────────────────────────────────────────────────────────────────
#  You have two options. Either:
#
#   (a) Pre-fill the block below (leave a value blank and you'll be prompted),
#   (b) Skip editing entirely and just export the variables before running:
#
#         export SN_INSTANCE=dev123456.service-now.com
#         export SN_USER=admin
#         export SN_PASS='...'          # or omit and you'll be prompted
#         ./fresh_instance.sh --full
#
#  The four values that MUST be right:
#
#   SN_INSTANCE  the hostname only -- no https://, no trailing slash.
#                Wrong here gives DNS errors or a 302 to a login page.
#
#   SN_USER      an account with admin rights. Creating TABLES and COLUMNS
#                requires admin on a stock instance; a lesser role fails with
#                403 on sys_db_object / sys_dictionary.
#
#   SN_PASS      quoted with SINGLE quotes if it contains $ ( ) # ! & or space.
#                Left blank -> prompted with echo off, never written to history.
#
#   FEED_URL     the source JSON. Must be a flat array of objects with an `id`.
#                If your source differs, edit FIELD_MAP in import_events.py too.
#
#  Everything below is safe to re-run. Every step is idempotent: tables are only
#  created if missing, columns are only added if missing, and the import upserts
#  on u_event_id so a second run writes nothing.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ═══════════════════════════════════════════════════════════════════════════
#  EDIT HERE (or leave blank and be prompted)
# ═══════════════════════════════════════════════════════════════════════════
SN_INSTANCE="${SN_INSTANCE:-}"          # e.g. dev123456.service-now.com
SN_USER="${SN_USER:-}"                  # e.g. admin
SN_PASS="${SN_PASS:-}"                  # leave blank -> secure prompt
SN_BASE_URL="${SN_BASE_URL:-}"          # optional; defaults to https://$SN_INSTANCE/api/now/table

FEED_URL="${FEED_URL:-https://findtorontoevents.ca/events.json}"
TARGET_TABLE="${TARGET_TABLE:-u_toronto_event}"   # must match specs/u_toronto_event.json
STAGING_TABLE="${STAGING_TABLE:-u_ev_stage}"      # pull-variant staging; harmless if unused
WORKERS="${WORKERS:-10}"                # parallel writers; 10 took 11,945 rows in ~230s
SAMPLE_SIZE="${SAMPLE_SIZE:-100}"       # rows used for the idempotency proof before a full load
# ═══════════════════════════════════════════════════════════════════════════

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

DO_FULL=0
DO_TESTS=0
CHECK_ONLY=0

usage() {
    sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
}

for arg in "$@"; do
    case "$arg" in
        --full)       DO_FULL=1 ;;
        --with-tests) DO_TESTS=1 ;;
        --check)      CHECK_ONLY=1 ;;
        --help|-h)    usage ;;
        *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
    esac
done

# ── pretty output ───────────────────────────────────────────────────────────
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32mok\033[0m   %s\n' "$*"; }
warn() { printf '   \033[33mwarn\033[0m %s\n' "$*"; }
die()  { printf '   \033[31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }

# ── 0. preflight: is this actually the repo? ────────────────────────────────
step "0. Checking the local checkout"
for f in sn_api.py create_table.py verify_table.py import_events.py; do
    [[ -f "$f" ]] || die "missing $f -- run this from the repo root"
done
command -v python3 >/dev/null || die "python3 not found on PATH"
ok "repo looks complete ($REPO_DIR)"
ok "python3: $(python3 --version 2>&1)"

# ── 1. collect parameters ───────────────────────────────────────────────────
step "1. Connection parameters"

# EDIT: instance hostname. Strip any scheme/trailing slash the user typed, because
# 'https://host/' silently produces 'https://https://host//api/...' downstream.
if [[ -z "$SN_INSTANCE" ]]; then
    read -r -p "   ServiceNow instance hostname (e.g. dev123456.service-now.com): " SN_INSTANCE
fi
SN_INSTANCE="${SN_INSTANCE#https://}"; SN_INSTANCE="${SN_INSTANCE#http://}"
SN_INSTANCE="${SN_INSTANCE%%/*}"
[[ -n "$SN_INSTANCE" ]] || die "instance is required"

# EDIT: username. Needs admin for schema creation.
if [[ -z "$SN_USER" ]]; then
    read -r -p "   Username: " SN_USER
fi
[[ -n "$SN_USER" ]] || die "username is required"

# EDIT: password. `-s` = no echo, so it never appears on screen or in history.
if [[ -z "$SN_PASS" ]]; then
    read -r -s -p "   Password for $SN_USER (input hidden): " SN_PASS
    echo
fi
[[ -n "$SN_PASS" ]] || die "password is required"

export SN_INSTANCE SN_USER SN_PASS
[[ -n "$SN_BASE_URL" ]] && export SN_BASE_URL

ok "instance : $SN_INSTANCE"
ok "user     : $SN_USER"
ok "password : ${#SN_PASS} characters (not displayed)"
ok "feed     : $FEED_URL"
ok "target   : $TARGET_TABLE"

# ── 2. persist to .env so later commands work in new shells ─────────────────
# .env is gitignored. Written 0600. This is the ONLY place the password is stored.
step "2. Writing .env (gitignored, mode 0600)"
umask 077
cat > .env <<ENVEOF
# Generated by fresh_instance.sh -- DO NOT COMMIT.
# This file is gitignored; keep it that way.
SN_INSTANCE=$SN_INSTANCE
SN_USER=$SN_USER
SN_PASS=$SN_PASS
ENVEOF
chmod 600 .env
ok ".env written"
git check-ignore -q .env && ok ".env is gitignored" || warn ".env is NOT gitignored -- fix .gitignore"

# ── 3. connectivity ─────────────────────────────────────────────────────────
step "3. Testing connectivity and credentials"
# sys_user is readable by any admin; a failure here is auth, DNS, or TLS.
if ! python3 sn_api.py count sys_user; then
    die "cannot read sys_user -- check SN_INSTANCE, SN_USER, SN_PASS, and that the account has API access"
fi

# A 400 from the Table API means "no such table" -- that is how absence is detected.
python3 sn_api.py probe "$TARGET_TABLE" || true

if [[ $CHECK_ONLY -eq 1 ]]; then
    step "Done (--check)"
    echo "   No writes were made to ServiceNow."
    echo "   A local .env was created (gitignored, mode 0600) so later commands work."
    exit 0
fi

# ── 4. schema ───────────────────────────────────────────────────────────────
step "4. Creating/reconciling the target table"
# Idempotent: creates only what is missing, then reports drift. Safe to re-run.
python3 create_table.py "specs/${TARGET_TABLE}.json"

step "5. Creating/reconciling the staging table (pull variant)"
python3 create_table.py "specs/${STAGING_TABLE}.json"

# ── 6. prove the schema ─────────────────────────────────────────────────────
step "6. Proving values survive a round trip"
# This is the highest-value check in the repo. It writes one probe row, reads it
# back, compares field by field, and deletes it. A dictionary can look perfect
# while silently truncating or rounding -- only a round trip reveals that.
# u_price_amount is declared as intentionally rounded to 2dp (currency).
python3 verify_table.py "$TARGET_TABLE" --ignore u_price_amount

# ── 7. prove idempotency before loading everything ──────────────────────────
step "7. Sample load: ${SAMPLE_SIZE} records"
python3 import_events.py --source "$FEED_URL" --limit "$SAMPLE_SIZE" \
    --workers "$WORKERS" --batch-id "fresh-sample-1" --no-sweep

step "8. Same sample again -- this MUST report inserted 0"
# If this inserts anything, the natural key is not guarding and a full load would
# duplicate the entire feed. Stop here and read docs/FINDINGS.md before --full.
python3 import_events.py --source "$FEED_URL" --limit "$SAMPLE_SIZE" \
    --workers "$WORKERS" --batch-id "fresh-sample-2" --no-sweep \
    | tee /tmp/fresh-idempotency.txt

if grep -qE '^  inserted +0$' /tmp/fresh-idempotency.txt; then
    ok "second run inserted 0 -- the key guards"
else
    die "second run inserted rows -- NOT idempotent. Do not run --full. See docs/FINDINGS.md"
fi

# ── 8. optional full load ───────────────────────────────────────────────────
if [[ $DO_FULL -eq 1 ]]; then
    step "9. Full feed load (this takes a few minutes)"
    python3 import_events.py --source "$FEED_URL" --workers "$WORKERS" \
        --batch-id "fresh-full-$(date -u +%Y%m%dT%H%M%SZ)" \
        --report "reports/fresh-full.json"

    step "10. Confirm a repeat full run writes nothing"
    python3 import_events.py --source "$FEED_URL" --workers "$WORKERS" \
        --batch-id "fresh-verify-$(date -u +%Y%m%dT%H%M%SZ)" \
        --report "reports/fresh-verify.json"
else
    warn "skipping the full load -- re-run with --full when ready"
fi

# ── 9. optional test suites ─────────────────────────────────────────────────
if [[ $DO_TESTS -eq 1 ]]; then
    step "11. Behavioural tests (these CLEAR the target table)"
    # NOTE: both suites clear and repopulate u_toronto_event. Never run two
    # suites at once against the same instance -- they will clobber each other
    # and produce false failures.
    python3 -u tests/test_sync.py --size 200 --workers 6
    python3 -u tests/test_loads.py --size 100 --loads 5
else
    warn "skipping test suites -- re-run with --with-tests (they clear the table)"
fi

# ── 10. what a script cannot do ─────────────────────────────────────────────
step "DONE -- but read this before you walk away"
cat <<'NEXT'

   Everything above is idempotent; re-running is safe.

   THREE THINGS ARE STILL MANUAL, because the platform forbids automating them:

   1. ACLs. A new table has NO ACLs, which means it is readable AND writable by
      every authenticated user.
        - avatar (top right) -> Elevate roles -> tick security_admin -> OK
        - System Security -> Access Control (ACL) -> New, twice:
              u_toronto_event   read    requires role: itil
              u_toronto_event   write   requires role: admin
      Why not scripted: POST sys_security_acl returns 403 "security constraints"
      even when the user holds security_admin. Elevation is a browser-session
      concept; REST has no equivalent. Verify with: python create_acl.py u_toronto_event

   2. A dedicated integration user (instead of admin), once you are past testing.
      Set its password in the UI -- REST writes to password fields are unhashed
      and always produce 401.

   3. The schedule, if you want it to run nightly.
        sudo cp deploy/sn-events-sync.service deploy/sn-events-sync.timer /etc/systemd/system/
        sudo install -m 0600 /dev/null /etc/sn-sync.env   # then paste SN_INSTANCE/USER/PASS
        sudo systemctl daemon-reload
        sudo systemctl enable --now sn-events-sync.timer
        systemctl list-timers sn-events-sync.timer
      The timer fires at 03:00 America/Toronto and catches up after downtime.
      Details, the EST-vs-EDT trap, and credential rotation: docs/SCHEDULING.md

   Next reads:
     docs/SCHEDULING.md                  triggers, rotation, JDBC variant
     docs/HOW_IT_TALKS_TO_SERVICENOW.md  auth + curl/PowerShell equivalents
     docs/REBUILD.md                     this process, condensed
     docs/FINDINGS.md                    what broke and how it was found

NEXT
