# Future work — deferred items

This file catalogs non-priority follow-ups that were deliberately not
done during the current sprint, so the backlog is visible and nothing
silently rots. Each entry follows the same shape: **what is deferred**,
**why it was deferred**, **trigger to revisit**, **cost estimate**.

The sibling file `docs/t09_postgres_parity_gap.md` is a fuller-depth
writeup of one specific deferral (T09 AC7 parity) — other entries are
summarized here and expanded to their own files only if they grow beyond
a paragraph.

---

## Authentication / actor attribution (TX1 `decided_by`)

**What:** `ApproveRequest`, `RejectRequest`, `CorrectRequest` accept a
`decided_by: str = "api_user"` field. When a caller does not pass it,
every approval decision is attributed to the literal string `"api_user"`
in the audit trail (Approval.decided_by and the APPROVAL_DECIDED
provenance event's actor). This is a placeholder, not a real identity.

**Why deferred:** The team needs to agree on the auth model before any
specific wiring lands. The leading candidate is **GlobusAuth** — we have
direct access to the Globus team and it pairs naturally with the planned
HPC export lanes (T04 Globus Compute, T05 PBS bundles also under Globus
umbrella infra). Shipping a stand-in auth now risks the eventual real
auth having a different actor-identity shape, creating a migration.

**Trigger to revisit:** any of
- GlobusAuth integration task is scheduled (would cover both HTTP session
  auth AND the MCP tool attribution question).
- A second user starts using the Control Plane in a shared deployment
  (current single-scientist laptop usage makes the placeholder tolerable).
- Audit evidence becomes load-bearing for compliance (at which point
  `"api_user"` rows are worse than useless — they launder identity).

**Cost estimate:** ~3–5 days, depending on how much of GlobusAuth's
flow we adopt vs. a minimal JWT-over-header shim. The handler changes
are small (~30 LOC across routes); the design conversation and token
lifecycle is the real cost.

---

## Pagination for list endpoints

**What:** `/approvals/pending` returns all matching rows in one response.
`/runs/list` accepts a `limit` field (default 50, max 500) but no cursor
or offset. Callers cannot page beyond the 500-item ceiling.

**Why deferred:** The target deployment is single-scientist on a laptop.
With realistic workflow volumes (~10–100 runs per scientist per month,
most terminating in 0 pending approvals), current limits are abundant.
Adding a cursor now is speculative.

**Trigger to revisit:**
- A deployment has >500 runs per user in the list-runs target window.
- An automation starts hitting `/approvals/pending` as a polling loop
  and occasionally sees >N (we'd notice in traces).
- A multi-user (shared Control Plane) deployment lands — pending
  approvals then aggregate across users and the numbers grow.

**Cost estimate:** ~1 day. Cursor-based pagination with a
`(created_at, id)` tuple works portably on both SQLite and Postgres.

---

## Postgres parity for TX1 HTTP tests

**What:** `tests/integration/test_api_approvals.py`,
`tests/integration/test_api_status.py`, and
`tests/integration/test_client_happy_paths.py` exercise the FastAPI
`TestClient` against a migrated **SQLite** file. Same tests do not run
against a live Postgres.

**Why deferred:** T09 AC7 already proves that migrations, the circular
FK constraint, and `ProvenanceRecorder` hash chaining all work on a real
Postgres. The TX1 route handlers contain no dialect-specific SQL — they
go through the SQLAlchemy ORM exclusively. The marginal information
gained by running the HTTP suite on Postgres is low; the cost of
setting up per-test schema isolation on a live Postgres is non-zero.

**Trigger to revisit:** any of
- A TX1 route grows a Postgres-only path (e.g., `FOR UPDATE`, advisory
  locks, window functions). The handler then has Postgres-specific
  behavior untestable on SQLite.
- Multi-process deployment lands. The ProvenanceRecorder's per-process
  `threading.Lock` no longer suffices; testing the DB advisory-lock
  escalation requires real Postgres.
- A bug is reported that reproduces on Postgres but not SQLite. (Best
  case this surfaces a dialect-drift the ORM layer papered over.)

**Cost estimate:** ~1 day. Parametrize the `cp_engine` fixture with
`["sqlite", "postgres"]`; skip the Postgres variant when the container
URL env var is unset; reuse the `clean_postgres` schema-drop fixture
from `test_postgres_parity.py` to isolate each test.

See `docs/t09_postgres_parity_gap.md` for the broader T09 parity gap
(crash-atomicity on Postgres, backup-restore on Postgres,
tamper-detection parity).
