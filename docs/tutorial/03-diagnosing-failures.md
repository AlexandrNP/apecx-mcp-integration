# 03 — Diagnosing failures

Goal: when a Run ends in ``failed``, know what to look at.

The LocalExecutor catches three failure classes and records each
as a structured provenance event. Every failure mode has a
specific symptom and a specific next step.

## 1. `workflow_misconfigured`

**Symptom**: ``reason`` contains "workflow_misconfigured".

**What happened**: the Run preconditions were wrong — no
``workflow_config_id``, the Artifact row referenced doesn't exist,
or its on-disk file is missing.

**Fix**: this should never happen through the HTTP API. If you see
it, something wrote to the DB outside the API. Query:

```sql
SELECT id, status, workflow_config_id FROM run WHERE id = '<run id>';
SELECT id, location FROM artifact WHERE id = '<workflow_config_id>';
-- Check that location file actually exists on disk.
```

## 2. `load_failed`

**Symptom**: ``reason`` starts with "workflow load failed: ...".

**What happened**: ``nanobrain.core.workflow.Workflow.from_config``
rejected the composed YAML. The LLM produced something nanobrain
can't instantiate. Common causes:

- LLM invented a class path that doesn't exist (e.g.
  ``nanobrain.library.workflows.viral_protein_analysis.utils.transform_data_unit_to_dict``
  — ``utils`` doesn't exist at that path).
- LLM used an inline ``config: {...}`` with a data-unit class name
  that doesn't exist (e.g., ``nanobrain.core.data_unit.TextDataUnit``).
- A link's ``source`` / ``target`` references a data unit name that
  doesn't exist on its step.

**Fix**: these are LLM-drift failures. Look at the composed YAML
via ``/workflows/diff`` and see which class the LLM emitted. If
you see the patterns above, the fix is in
``src/apecx_integration/composition/composer_prompts/system.md`` —
the system prompt is load-bearing for constraining these drifts.
See ``CLAUDE.md`` § "Composer prompt engineering is load-bearing."

## 3. `execute_failed`

**Symptom**: ``reason`` starts with "workflow execution failed: ...".

**What happened**: nanobrain loaded the workflow fine, but a step's
``process()`` raised. Common causes:

- Ollama unreachable mid-run (the ``extract_entities_llm`` step
  calls the configured LLM; if it's gone, that step fails).
- A BV-BRC snapshot file is missing from ``data/bvbrc_cache/``.
- An apecx_db_integration dependency (pandas, langchain) raised.

**Fix**:

1. Check Ollama is running: ``curl -s http://localhost:11434/api/tags``.
2. Check ``APECX_LLM_*`` env vars match the Control Plane process.
3. Look at Control Plane logs for the full traceback — the HTTP
   response truncates it.

## How to re-run after a fix

Runs are **append-only**. You don't re-run the same Run after
failure; you start a new one:

```bash
# Compose again (same prompt, new Run)
curl -s http://localhost:8000/workflows/start \
  -H 'Content-Type: application/json' \
  -d "{\"description\": \"$PROMPT\", \"user_id\": \"alex\"}" | jq .
```

The failed Run stays in the DB with its provenance chain intact —
that's the audit. Scientists can diff a failed Run against a
succeeding one via ``/workflows/diff`` (compare the two
``yaml_text`` outputs).

Next file: `04-reproducing.md`.
