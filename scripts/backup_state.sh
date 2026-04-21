#!/usr/bin/env bash
# T09 step 5: back up the Control Plane state DB.
#
# Reads APECX_CP_DB_URL (default sqlite:///./apecx_cp.db).
# Writes backup to the path given as the first argument.
#
# SQLite: uses sqlite3's `.backup` command, which takes a consistent
#   snapshot via the SQLite backup API (correctly handles WAL + any
#   in-progress writers — a raw `cp` can produce a corrupt copy when
#   journal_mode=WAL).
# Postgres: uses `pg_dump -Fc` (custom format; compressed, fast restore).

set -euo pipefail

DB_URL="${APECX_CP_DB_URL:-sqlite:///./apecx_cp.db}"
DEST="${1:-}"
if [[ -z "$DEST" ]]; then
    echo "Usage: $0 <dest_path>" >&2
    exit 2
fi

case "$DB_URL" in
    sqlite:*)
        DB_FILE="${DB_URL#sqlite:///}"
        if [[ ! -f "$DB_FILE" ]]; then
            echo "SQLite file not found: $DB_FILE" >&2
            exit 1
        fi
        sqlite3 "$DB_FILE" ".backup '$DEST'"
        echo "SQLite backup -> $DEST"
        ;;
    postgresql:*|postgres:*)
        pg_dump -Fc -f "$DEST" "$DB_URL"
        echo "Postgres dump -> $DEST"
        ;;
    *)
        echo "Unsupported URL scheme: $DB_URL" >&2
        exit 2
        ;;
esac
