#!/usr/bin/env bash
#
# run-sync.sh -- cron-friendly wrapper around import_events.py.
#
# Use this if you are scheduling with cron instead of a systemd timer.
#
#   CRON_TZ=America/Toronto
#   0 3 * * * /opt/sn-db-repo/deploy/run-sync.sh >> /var/log/sn-sync/cron.log 2>&1
#
# Credentials: /etc/sn-sync.env (mode 0600). Never inline them here.

set -euo pipefail

ENV_FILE="${SN_ENV_FILE:-/etc/sn-sync.env}"
REPO_DIR="${SN_REPO_DIR:-/opt/sn-db-repo}"
LOG_DIR="${SN_REPORT_DIR:-/var/log/sn-sync}"
SOURCE="${SN_SOURCE:-https://findtorontoevents.ca/events.json}"
WORKERS="${SN_WORKERS:-10}"

if [[ ! -r "$ENV_FILE" ]]; then
    echo "FATAL: cannot read $ENV_FILE (expected SN_INSTANCE/SN_USER/SN_PASS)" >&2
    exit 2
fi

# Export every VAR=value in the env file.
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

for required in SN_INSTANCE SN_USER SN_PASS; do
    if [[ -z "${!required:-}" ]]; then
        echo "FATAL: $required is not set in $ENV_FILE" >&2
        exit 2
    fi
done

mkdir -p "$LOG_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

cd "$REPO_DIR"

# -u = unbuffered, so cron log shows progress instead of one dump at the end.
/usr/bin/python3 -u import_events.py \
    --source "$SOURCE" \
    --workers "$WORKERS" \
    --batch-id "cron-$STAMP" \
    --report "$LOG_DIR/report-$STAMP.json"

# ---------------------------------------------------------------------------
# Derive the two columns the dashboard depends on.
#
# import_events.py does not set these: u_primary_category (first CSV token of
# u_categories) and u_location_ref (venue -> cmn_location, which is what gives
# the map its coordinates). Without this step, every record the sync INSERTs
# would be missing from the category chart and invisible on the map until
# somebody ran the backfill by hand. Found the hard way: three rows had drifted
# three hours after the last manual run.
#
# The script is idempotent -- it only PATCHes rows whose values are absent or
# stale -- so running it after every sync costs one scan.
/usr/bin/python3 -u backfill_geo_category.py --workers "$WORKERS" \
    || echo "WARN: derived-field backfill failed; map/category will lag" >&2
