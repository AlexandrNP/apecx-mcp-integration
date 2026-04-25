# 02 — Reviewing results + the audit trail

Goal: pull the output artifact, walk the provenance chain, and
understand what you can trust.

## 1. Get the Run's current state

```bash
curl -s http://localhost:8000/runs/status \
  -H 'Content-Type: application/json' \
  -d "{\"run_id\": \"$RUN_ID\"}" | jq .
```

``status`` is one of ``pending`` / ``running`` / ``paused`` /
``completed`` / ``failed`` / ``cancelled``. After chapter 01's
execute, you should see ``completed``.

## 2. Pull the output artifact

The Tier-2 API exposes artifacts by id (``POST /runs/artifact``
takes ``{"artifact_id": "<uuid>"}``); there is no list-by-run
route today. That's a known product gap (T15 follow-up). The
tutorial walks you through fetching the two artifacts you already
know about from chapter 01:

- The composed-workflow artifact id is the
  ``generated_workflow_artifact_id`` field of
  ``/workflows/start``'s response (also surfaced as
  ``run.workflow_config_id``).
- The output-artifact id is the ``output_artifact_id`` field of
  ``/workflows/execute``'s response.

```bash
# Fetch the composed-workflow artifact.
WORKFLOW_ARTIFACT_ID=<generated_workflow_artifact_id from /workflows/start>
curl -s http://localhost:8000/runs/artifact \
  -H 'Content-Type: application/json' \
  -d "{\"artifact_id\": \"$WORKFLOW_ARTIFACT_ID\"}" | jq .

# Fetch the output artifact.
OUTPUT_ARTIFACT_ID=<output_artifact_id from /workflows/execute>
curl -s http://localhost:8000/runs/artifact \
  -H 'Content-Type: application/json' \
  -d "{\"artifact_id\": \"$OUTPUT_ARTIFACT_ID\"}" | jq .
```

Each response has ``artifact.kind`` (``generated_workflow`` or
``output``), ``artifact.content_hash`` (sha256 hex), an
``artifact.location`` on disk, and an optional ``inline_bytes``
(present for small artifacts; check ``reason_inline_omitted`` if
it's null).

If you want to discover ALL artifacts for a run without the prior
ids, query the SQLite DB directly:

```bash
sqlite3 ./tut_cp.db \
  "SELECT id, kind, location FROM artifact WHERE run_id = '$RUN_ID';"
```

(Replace ``./tut_cp.db`` with your ``APECX_CP_DB_URL`` target.
For Postgres, use ``psql`` with the equivalent SELECT.) A
``GET /runs/<run_id>/artifacts`` route is queued as a T15
follow-up so the tutorial can drop the SQL escape hatch.

## 3. Validate the provenance chain

Every action on a Run writes a provenance event with a hash that
covers ``(prev_event_hash, run_id, event_type, actor, timestamp,
payload)``. The chain is validate-able.

```bash
curl -s http://localhost:8000/metrics/approvals?since=2026-01-01T00:00:00Z | jq .
```

That endpoint aggregates time-to-decide from the chain; the raw
events aren't exposed via HTTP today but `sqlite3` or the
ProvenanceRecorder's `validate(run_id)` do the walk directly:

```python
# .venv/bin/python
from uuid import UUID
from apecx_integration.control_plane.db import make_engine, make_session_factory
from apecx_integration.control_plane.provenance.recorder import ProvenanceRecorder

engine = make_engine()  # reads APECX_CP_DB_URL
recorder = ProvenanceRecorder(make_session_factory(engine))
recorder.validate(UUID("<run id>"))   # raises ChainBroken on any mismatch
```

A clean return means: every event references the previous event's
hash, every hash is recomputable from the stored payload, and no
ghost events have been inserted.

## 4. The MCP-tool equivalent

From Claude Desktop (or any MCP client that wires the
`apecx-mcp` server):

```
tool: execute_workflow
  arg: run_id = "<run id>"
```

Returns the same terminal-state JSON. Tools available:
``start_workflow`` / ``show_diff`` / ``execute_workflow`` /
``list_pending_approvals`` / ``approve`` / ``reject`` / ``correct``
/ ``estimate_cost`` / ``confirm_allocation`` /
``export_hpc_bundle`` / ``ingest_hpc_bundle``.

## What "trust this artifact" means

Three things hold if the chain validates:

1. The Run's composition was produced by the LLM model recorded in
   ``GeneratedArtifact.llm_model_version_hash`` at the time
   ``created_at`` says.
2. Any novel Python that shipped in the bundle was scanned by the
   T13 import whitelist before persist — no dynamic imports, no
   banned constructs.
3. Every approval decision names a human ``decided_by``, records a
   ``comment``, and happened before execution resumed.

If the chain DOESN'T validate, something tampered with the DB
outside the API — the artifact should not be trusted.

Next file: `03-diagnosing-failures.md`.
