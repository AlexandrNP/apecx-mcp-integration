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

```bash
# List every artifact tied to this run
curl -s http://localhost:8000/runs/artifact \
  -H 'Content-Type: application/json' \
  -d "{\"run_id\": \"$RUN_ID\"}" | jq .
```

You'll see at least two:

- ``kind: generated_workflow`` — the composed YAML (same one
  ``/workflows/diff`` surfaced).
- ``kind: output`` — the result JSON the LocalExecutor persisted
  when execution completed.

Each artifact has a ``content_hash`` (sha256 hex) and a
``location`` on disk.

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
