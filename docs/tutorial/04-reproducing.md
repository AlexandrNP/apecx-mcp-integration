# 04 — Reproducing and sharing a run

Goal: turn a completed Run into an artifact someone else can run,
with enough provenance to trust the result.

## What "reproducing" means here

A reproducible artifact has four properties:

1. **Deterministic given the same inputs.** Same prompt + same
   library version + same pinned LLM version should produce the
   same generated workflow YAML.
2. **Re-executable.** The workflow YAML can be re-instantiated and
   run against the same snapshot data.
3. **Identifiable.** Each artifact carries a content hash; each Run
   carries a hash-chained provenance log.
4. **Transferable.** The whole bundle fits in a directory a
   scientist can ``rsync`` to a colleague.

The T12 reproducibility harness checks (1). T11 enforces (3) and
(4) at the Control Plane layer. (2) is what this chapter walks you
through.

## 1. Export a bundle

If you want to run on HPC, ``/hpc/export`` packages the Run into a
qsub-able directory:

```bash
curl -s http://localhost:8000/hpc/export \
  -H 'Content-Type: application/json' \
  -d "{
    \"run_id\": \"$RUN_ID\",
    \"target_system\": \"polaris\",
    \"output_directory\": \"/tmp/run_$RUN_ID\"
  }" | jq .
```

You get back a ``bundle_path`` containing:

- ``submit.pbs`` — PBS script with directives pre-filled. Edit
  ``-A <FILL_IN_ALLOCATION_ACCOUNT>`` before qsubbing.
- ``run.sh`` — invoked inside the PBS job; loads env + runs the
  workflow.
- ``workflow.yml`` — the composed YAML, verbatim.
- ``staging_plan.yml`` — input references (by content hash).
- ``provenance_seed.json`` — enough metadata for Tier-2 to
  reconstruct the Run row on completion.
- ``README.md`` — what's inside, how to submit, how to transfer
  results back.

Transfer the bundle directory to the target HPC, ``qsub``, and
wait for the job.

## 2. Re-ingest the result

After the HPC job finishes and you've transferred the bundle
directory back (with populated ``outputs/result.json`` +
``apecx_status.txt``), re-ingest:

```bash
curl -s http://localhost:8000/hpc/ingest \
  -H 'Content-Type: application/json' \
  -d "{\"bundle_path\": \"/tmp/run_$RUN_ID\"}" | jq .
```

The Control Plane reads ``provenance_seed.json``, flips the Run to
``completed`` (or ``failed``), persists ``outputs/result.json`` as
an OUTPUT Artifact, and emits the RUN_COMPLETED / RUN_FAILED
provenance event. The hash chain now spans laptop → HPC → laptop.

## 3. Cost estimate + confirmation gate

Before committing HPC cycles, get an allocation estimate:

```bash
curl -s http://localhost:8000/hpc/estimate \
  -H 'Content-Type: application/json' \
  -d "{\"run_id\": \"$RUN_ID\"}" | jq .
# → { "total_core_hours": 0.42, "per_step_core_hours": {...}, ... }

# Approve the estimate
curl -s http://localhost:8000/hpc/confirm \
  -H 'Content-Type: application/json' \
  -d "{\"run_id\": \"$RUN_ID\", \"confirmed_core_hours\": 0.5}" | jq .
```

Confirmation records a user-acknowledgement before the scientist
qsubs. The audit trail shows estimate → confirmation →
(submission elsewhere) → ingest.

## 4. Hand the whole thing to a colleague

A colleague re-runs the same workflow with:

```bash
# Fetch the workflow YAML from your Run
curl -s http://localhost:8000/workflows/diff \
  -H 'Content-Type: application/json' \
  -d "{\"run_id\": \"$RUN_ID\"}" | jq -r '.yaml_text' > their_workflow.yml

# They re-instantiate it against their Control Plane — not
# documented here since "colleague runs a local Control Plane" is
# chapter 00 territory.
```

Or they ``rsync`` the HPC bundle and ingest it into their own
Control Plane via the same ``/hpc/ingest`` call.

## Reproducibility guarantees — what's actually tested

- ``tests/reproducibility/test_baselines.py`` asserts 3 shipped
  fixtures produce their captured baseline hashes every time.
  Baseline hashes are recorded against the current composer
  pipeline + canned LLM response; they catch pipeline drift.
- Live-LLM reproducibility is **not** CI-tested on this repo —
  temperature=0 isn't 100% deterministic across Ollama runs
  (probability ties can flip). Operators who want live-LLM
  baselines add fixtures without ``canned_response.txt`` and
  capture hashes against a pinned model snapshot.

That's it. You've composed, reviewed, approved, executed,
diagnosed, and reproduced. The rest is domain-specific workflow
authoring.
