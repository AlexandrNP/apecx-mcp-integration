# 01 — Your first workflow

Goal: compose a workflow from a natural-language prompt, review it,
approve it, and execute it. End-to-end, under 5 minutes after
setup.

## The scientist prompt

We'll ask the composer to build a workflow that extracts pathogen
entity names from a user query and maps them against the local
BV-BRC snapshot. This is the canonical violin_bvbrc workflow; it's
in scope of the shipped component catalog.

## 1. Compose + persist a Run

```bash
curl -s http://localhost:8000/workflows/start \
  -H 'Content-Type: application/json' \
  -d '{
    "description": "Extract pathogen entity names from a biomedical query and map them to BV-BRC genome ids using the local snapshot.",
    "user_id": "alex",
    "preferred_executor": "local"
  }' | jq .
```

You get back a ``run`` object. Note:

- ``run.id`` — you'll use this below.
- ``run.status`` — either ``running`` (auto-approved; fully composed
  from library components) or ``paused`` (the composer emitted novel
  Python that needs human review per the T06 policy).
- ``generated_workflow_artifact_id`` — the Artifact row holding the
  composed YAML.

Typical first-try: under 60 seconds against mistral-nemo. If it
takes longer, the LLM is cold-loading; retries after are ~30s.

## 2. See what was composed

```bash
RUN_ID=<the id from step 1>

curl -s http://localhost:8000/workflows/diff \
  -H 'Content-Type: application/json' \
  -d "{\"run_id\": \"$RUN_ID\"}" | jq .
```

You'll see:

- ``yaml_text`` — the composed workflow YAML, verbatim.
- ``novel_python_by_step`` — step_id → source for any novel Python
  the LLM produced. Empty dict when the workflow is 100% composition
  (the preferred outcome).
- ``categorization`` — one entry per step with a per-step
  ``category`` of ``composed_standard`` / ``composed_parameterized``
  / ``composed_wrapped`` / ``novel``.
- ``summary_sentence`` — plain-English review-UX string: "This
  workflow has 4 step(s). 4 compose library components (3 standard
  + 1 parameterized + 0 wrapped). 0 step(s) are novel Python
  requiring review."

## 3. Approve (if paused)

If ``run.status == "paused"``, find + decide the pending approval:

```bash
curl -s http://localhost:8000/approvals/pending \
  -H 'Content-Type: application/json' \
  -d '{"user_id": "alex"}' | jq '.approvals[0]'

APPROVAL_ID=<approval.id from above>

curl -s http://localhost:8000/approvals/approve \
  -H 'Content-Type: application/json' \
  -d "{\"approval_id\": \"$APPROVAL_ID\", \"comment\": \"looks fine\", \"decided_by\": \"alex\"}" | jq .
```

If ``run.status`` was ``running`` already, skip this step.

## 4. Execute the workflow

```bash
curl -s http://localhost:8000/workflows/execute \
  -H 'Content-Type: application/json' \
  -d "{\"run_id\": \"$RUN_ID\"}" | jq .
```

Response:

- ``status: "completed"`` + ``output_artifact_id`` — the workflow
  ran to completion. The result artifact is on disk; see chapter 02
  for how to retrieve it.
- ``status: "failed"`` + ``reason`` — the LocalExecutor captured a
  clean failure. ``reason`` names the failure class:
  ``workflow load failed`` / ``workflow execution failed`` /
  ``workflow_misconfigured``. On mistral-nemo this path is rare but
  possible; see chapter 03 for how to diagnose.

## What just happened

You drove a scientist-level workflow end-to-end through four
tiers:

- **Tier 1 (MCP / HTTP)** — you hit the Control Plane directly; the
  MCP server wraps these calls for Claude Desktop.
- **Tier 2 (Control Plane)** — persisted Run, Artifact, approval,
  and provenance events in a hash-chained audit log.
- **Tier 3 (Composition)** — the LLM composer matched your prompt
  to library components; the differ categorized each step.
- **Tier 4 (Execution)** — the LocalExecutor loaded the composed
  YAML into nanobrain's Workflow machinery and ran it.

Next file: `02-reviewing-results.md`.
