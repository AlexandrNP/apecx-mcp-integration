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

## ⚠ Phase-2 scaffold caveat — read before running anything below

The HPC export + ingest plumbing is **shipped end-to-end at the
data-plane layer**, but the inner ``run.sh`` produced by the
exporter is a **scaffold that does not actually execute the
workflow**. T05 (the real qsub-driven runner that loads nanobrain
and runs the composed workflow) is still pending. Until it lands:

- Exported bundles are well-formed and will ``qsub`` cleanly.
- ``run.sh`` writes a ``stub_completed`` marker and exits 0. It
  produces a placeholder ``outputs/result.json`` with
  ``{"status": "stub_completed", "stub": true}``.
- The Control Plane's ingest path **detects the stub marker** and
  marks the Run as ``failed`` with reason
  ``stub_bundle_detected``. This is intentional, not a bug —
  surfacing the stub as a failure is what prevents a scientist
  from believing fake successful HPC runs.
- The hash chain still spans laptop → HPC → laptop, and every
  step is provenance-logged. So this chapter still exercises the
  contract end-to-end, even if the inner workflow is a no-op.

When T05 lands, ``run.sh`` will write ``completed`` (not
``stub_completed``) and the ingest path's existing happy-path
branch will mark the Run COMPLETED. This chapter will be
re-validated then. See ``docs/codebase_audit_2026_04_24.md`` §3.5
for context.

## 1. Export a bundle

``/hpc/export`` packages the Run into a qsub-able directory:

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
- ``run.sh`` — invoked inside the PBS job. **Stub script today**
  (see caveat above): writes ``stub_completed`` marker and exits
  0. Will run the real workflow when T05 lands.
- ``workflow.yml`` — the composed YAML, verbatim.
- ``staging_plan.yml`` — input references (by content hash).
- ``provenance_seed.json`` — enough metadata for Tier-2 to
  reconstruct the Run row on completion.
- ``README.md`` — what's inside, how to submit, how to transfer
  results back.

Transfer the bundle directory to the target HPC, ``qsub``, and
wait for the job. With today's stub, the job completes in
seconds.

## 2. Re-ingest the result

After the HPC job finishes and you've transferred the bundle
directory back (with populated ``outputs/result.json`` +
``apecx_status.txt``), re-ingest:

```bash
curl -s http://localhost:8000/hpc/ingest \
  -H 'Content-Type: application/json' \
  -d "{\"bundle_path\": \"/tmp/run_$RUN_ID\"}" | jq .
```

The Control Plane reads ``provenance_seed.json``, then branches
on the marker in ``apecx_status.txt``:

- ``completed`` → Run flipped to ``completed``,
  ``outputs/result.json`` persisted as an OUTPUT Artifact,
  ``RUN_COMPLETED`` provenance event emitted. (This is the path
  the real T05 runner will take when it lands.)
- ``failed`` → Run flipped to ``failed``, ``RUN_FAILED`` event
  emitted with ``reason: remote_failure``.
- ``stub_completed`` (today's default) → Run flipped to
  ``failed``, ``RUN_FAILED`` event emitted with
  ``reason: stub_bundle_detected``. No OUTPUT Artifact is
  persisted because the stub didn't produce a real one.

The hash chain spans laptop → HPC → laptop in all three cases.

If a concurrent ingest beats yours to the same run (the second
ingester sees the run already in a terminal state), the response
is ``409 Conflict`` with a clear "concurrent ingest" detail
message. The ingest path uses an atomic conditional UPDATE so
the loser's bundle never partially writes.

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

- ``tests/reproducibility/test_baselines.py`` asserts **10
  placeholder-LLM fixtures** produce their captured baseline
  hashes every time. Three are virus-domain (the original
  T12 seeds against the violin × bv_brc workflow); seven are
  generic (1-step empty-links, multi-step chains, novel-Python-
  only, library-plus-novel-Python, etc.) covering the diversity
  matrix in ``tests/reproducibility/README.md``. Baseline hashes
  are recorded against the current composer pipeline + canned
  LLM responses; they catch pipeline drift (parser changes,
  scanner-whitelist shifts, YAML serialization).
- Live-LLM reproducibility is **not** CI-tested on this repo —
  temperature=0 isn't 100% deterministic across Ollama runs
  (probability ties can flip). Operators who want live-LLM
  baselines add fixtures without ``canned_response.txt`` and
  capture hashes against a pinned model snapshot. The harness
  auto-skips those unless ``APECX_T12_RUN_LIVE_LLM=1`` is set.
- ``scripts/capture_fixture_baselines.py`` automates the hash
  capture for placeholder-LLM fixtures using the composer's own
  fence-extraction regex, so you can't accidentally produce a
  hash that doesn't match what the composer would compute at
  runtime.

That's it. You've composed, reviewed, approved, executed,
diagnosed, and reproduced. The rest is domain-specific workflow
authoring.
