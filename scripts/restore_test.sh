#!/usr/bin/env bash
# T09 step 6: restore a Control Plane backup and verify it is queryable.
#
# Required tools on $PATH:
#   - sqlite3                 (SQLite backups)
#   - pg_restore + psql       (Postgres backups)
#   - head, od                (basic coreutils, for magic-byte detection)
# See backup_state.sh for install guidance.
#
# Auto-detects backup format (SQLite magic bytes vs. pg_dump magic) and
# restores to either:
#   - a local SQLite file (arg 2, default /tmp/apecx_cp_restored.db)
#   - a Postgres URL given by env var APECX_CP_RESTORED_URL (must exist,
#     will be pg_restore'd into)
#
# Verification: prints the row count from `run` (the core table). The
# caller (integration test) compares this against the expected count to
# confirm the round-trip preserved data.

set -euo pipefail

BACKUP="${1:-}"
VERIFY_DB_PATH="${2:-/tmp/apecx_cp_restored.db}"

if [[ -z "$BACKUP" ]]; then
    echo "Usage: $0 <backup_path> [verify_db_path_for_sqlite]" >&2
    exit 2
fi
if [[ ! -f "$BACKUP" ]]; then
    echo "Backup file not found: $BACKUP" >&2
    exit 1
fi

MAGIC=$(head -c 16 "$BACKUP" || true)

if [[ "$MAGIC" == "SQLite format 3"* ]]; then
    # SQLite backup is just a valid SQLite file; copying is the restore.
    cp "$BACKUP" "$VERIFY_DB_PATH"
    COUNT=$(sqlite3 "$VERIFY_DB_PATH" "SELECT COUNT(*) FROM run;")
    echo "SQLite restore OK ($VERIFY_DB_PATH): run rows = $COUNT"
elif [[ "${MAGIC:0:5}" == "PGDMP" ]]; then
    if [[ -z "${APECX_CP_RESTORED_URL:-}" ]]; then
        echo "Postgres backup detected but APECX_CP_RESTORED_URL not set" >&2
        exit 2
    fi
    pg_restore --clean --if-exists --no-owner -d "$APECX_CP_RESTORED_URL" "$BACKUP"
    COUNT=$(psql -At "$APECX_CP_RESTORED_URL" -c "SELECT COUNT(*) FROM run;")
    echo "Postgres restore OK ($APECX_CP_RESTORED_URL): run rows = $COUNT"
else
    echo "Unknown backup format (magic: $(od -c <<<"$MAGIC" | head -1))" >&2
    exit 3
fi
