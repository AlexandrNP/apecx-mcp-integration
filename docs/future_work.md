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

---

## Apptainer Postgres runtime image

**What:** The Apptainer runtime is wired and its plumbing is proven end-
to-end via the Lima integration tests (lightweight Alpine image),
but `ApptainerRuntime.ensure_postgres_running(config)` with the default
`image="postgres:16-alpine"` does **not** yield a running Postgres. The
`tests/integration/test_apptainer_runtime.py::test_postgres_accepts_connections_under_apptainer`
test is `xfail(strict=True)` with a pointer to this entry — the moment a
working image lands, that test flips to pass and CI flags it.

**Why it doesn't work:** `apptainer instance start docker://postgres:16-alpine`
spawns Apptainer's own `appinit` as PID 1 inside the container instead of
invoking the image's ENTRYPOINT. The Postgres `docker-entrypoint.sh`
never runs, so no `initdb`, no `postgres` listener. This is an
Apptainer↔Docker semantic difference, not a bug in our wrapper: `instance
start` expects the image to define `%startscript` (SIF format only), not
a Docker ENTRYPOINT. Additionally, the Postgres Docker image assumes
root-owned `gosu postgres` dropdown — a pattern that does not survive
Apptainer's unprivileged user-namespace execution model.

**Why deferred:** Docker is sufficient on scientist laptops (the priority
deployment). Apptainer is for HPC, and HPC users who actually need
Postgres under Apptainer will need a custom SIF image anyway — a generic
`docker://postgres` wouldn't be the right artifact in that environment
either (storage locations, shared filesystems, user-namespace quirks all
vary per cluster).

**Trigger to revisit:** any of
- A paying user sits on an HPC node and wants to run the Control Plane
  there with Apptainer-managed Postgres.
- We find that SQLite-on-HPC-shared-filesystem misbehaves (lock issues,
  WAL file semantics on NFS/Lustre) at which point BYO-Postgres or a
  custom SIF becomes the path.

**Cost estimate:** ~3–5 days.
- Build an Apptainer recipe (`.def`) file that bakes Postgres configured
  for a non-root user (e.g., `postgres-nonroot.sif`).
- Define a `%startscript` that invokes `postgres` with the appropriate
  `PGDATA` under the bind-mount.
- Update `ApptainerRuntime` to default `image="file:///path/to.sif"` or
  accept an `image_uri` override (probably already works — verify).
- Run the xfail test on a cluster + verify.

---

## Teardown race: ensure-immediately-after-teardown

**What:** `teardown_infra(db_url)` calls the runtime's `teardown` then
returns. It does not block until the container is guaranteed-gone; both
`docker compose down` (usually synchronous) and `apptainer instance stop`
(async-ish) can leave transient state where a subsequent
`ensure_infra_ready(db_url)` races.

**Why deferred:** In normal scientist usage (`apecx-cp serve` → Ctrl-C →
`apecx-cp serve` again) there is plenty of wall-clock between the two,
so the race is a theoretical risk. We have no reported occurrences.

**Trigger to revisit:** any automated orchestration that does
back-to-back `teardown → ensure` (e.g., a `reset-db` subcommand, a test
harness that cycles infra). Also if a user reports mysterious
"container name already in use" errors after `apecx-cp teardown`.

**Mitigation strategies, in order of invasiveness:**
- **Sleep-and-poll wrapper around teardown.** `teardown_infra` polls
  `is_postgres_running()` until false (or timeout) before returning.
  Cheap, ~10 LOC.
- **Docker: use `docker compose down --wait`.** Compose blocks until
  containers are actually removed. For Apptainer, wait-loop on
  `apptainer instance list`.
- **Retry-with-backoff inside `ensure_postgres_running`.** If the
  `up -d` call fails with "container name in use" or similar, sleep
  briefly and retry. More robust but hides real bugs.

**Cost estimate:** ~0.5 day (option 1), ~1 day (option 2).

---

## Stress / concurrent-startup tests for infra governance

**What:** No test exercises two `apecx-cp serve` processes starting
simultaneously, or one `serve` invocation interrupted mid-startup
and a second one begun. Compose's project-level lockfile should
handle this gracefully, but we have not verified.

**Why deferred:** Pathological case. Single-user laptop target makes
it unlikely.

**Trigger to revisit:** multi-user shared deployment, or a CI pipeline
that parallelizes apecx-cp serve across test matrices.

**Cost estimate:** ~0.5 day. Spawn two subprocess.Popen instances of
`apecx-cp serve` with staggered delays; verify both converge to the
same healthy Postgres without error.

---

## Lima-on-macOS caveats for Apptainer

**What:** Apptainer-on-macOS via Lima has a few sharp edges documented
in `tests/integration/test_apptainer_runtime.py` but worth escalating:
- Lima reverse-mounts `$HOME` **read-only** by default. Apptainer
  bind-mounts need writable host paths, so tests use `/tmp/` inside
  the VM, not host `tmp_path`.
- `/private/var/folders/.../tmp` (macOS pytest tmp dir) isn't mounted
  inside the VM at all.
- Lima's default port-forward picks up anything on the VM's
  `127.0.0.1`, but the set is bounded — large-port setups may need
  explicit `portForwards` config.

**Why deferred:** We got the tests working around these; no code
action needed unless the test matrix grows.

**Trigger to revisit:** If a non-macOS / non-HPC platform emerges that
needs containerized Apptainer (WSL? Windows? — unlikely).

**Cost estimate:** 0 unless required.
