# T09 AC7 — Postgres parity gap (deferred)

Status: **non-priority future work** as of 2026-04-21.

## What AC7 currently covers

`tests/integration/test_postgres_parity.py` runs on a live Postgres
(via `docker-compose up -d postgres`) and verifies:

- `alembic upgrade head` + `downgrade -1` + `upgrade head` round-trip on
  Postgres. Catches the circular-FK topological-order bug that the
  `use_alter` + `batch_alter_table('run').create_foreign_key(...)` split
  fixed.
- The circular FK `run.workflow_config_id -> artifact.id` is enforced by
  Postgres (raises `IntegrityError` on orphan insert).
- `ProvenanceRecorder` hash-chain roundtrips cleanly on Postgres;
  `_canonical_timestamp` normalization gives stable hashes across
  dialects.

## What AC7 does NOT cover

| Test file | Covers on Postgres? | Reason |
|---|---|---|
| `test_migrations.py` | duplicated by `test_postgres_parity.py::test_alembic_roundtrip_on_postgres` | effectively covered |
| `test_db_wal.py` | **NO** | WAL is a SQLite-specific journal mode; has no Postgres equivalent to test here |
| `test_provenance_chain.py` | partially (one happy-path test on Postgres) | tamper-detection + per-run isolation tests run only against SQLite |
| `test_crash_recovery.py` | **NO** | SIGKILL-atomicity test runs only against SQLite. Postgres is almost certainly fine (its WAL + crash recovery are mature) but we have not proven it against this schema on this code |
| `test_backup_restore.py` | **NO** | Postgres path is scripted (`pg_dump` / `pg_restore`) but no integration test exercises the round-trip |

## Impact

Low. The schema is dialect-neutral (portable enum-as-VARCHAR, UUIDString
TypeDecorator, JSON columns), and the one place dialect mattered (the
circular FK) is live-tested on Postgres. The remaining gaps are
behavioral parity of Python-side code (ProvenanceRecorder tamper
detection) and operational parity of scripts (backup/restore on
Postgres) — areas where Postgres is mature enough that SQLite-only tests
are a reasonable proxy for a v1 shipment.

## Triggers to revisit

Promote this from "non-priority" to "do now" if any of:

- The Control Plane deployment target becomes Postgres (not SQLite) for
  real users. Then the crash-atomicity story needs live Postgres proof.
- A Postgres-only schema feature is added (e.g., partial indexes,
  materialized views). Those cannot be mirrored on SQLite, and any
  Postgres-specific behavior needs its own integration test.
- The ProvenanceRecorder grows Postgres-specific paths (advisory locks,
  SERIALIZABLE escalation for multi-process). Those paths are by
  definition untested against SQLite and need live Postgres coverage.

## Cost estimate to close

~1.5 days of work:
- Parametrize the SQLite integration tests with a `db_url` fixture
  yielding both `sqlite:///...` and the Postgres URL; skip SQLite-only
  tests (WAL) on Postgres, skip Postgres-only tests on SQLite.
- Rewrite `test_backup_restore.py` to spawn a second container for the
  restore target (or reuse the same by dropping/recreating the DB).
- Rewrite `test_crash_recovery.py` to use `pg_terminate_backend` or
  `docker kill` as the Postgres-equivalent of SIGKILL — and verify the
  atomicity claim on Postgres specifically.

Not in scope for T09 shipment; not in scope for first release per
user directive 2026-04-21.
