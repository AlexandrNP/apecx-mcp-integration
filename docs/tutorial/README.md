# APECx tutorial

Five-chapter walkthrough from a clean laptop to a reproducible run.
Target: a scientist with zero prior exposure completes all five in
<90 minutes (AP §5.15 AC2, validated with a real scientist under
T15 Phase 3).

1. [00-setup.md](00-setup.md) — install deps, start Ollama +
   Control Plane + MCP server.
2. [01-first-workflow.md](01-first-workflow.md) — compose a
   workflow from natural language, review, approve, execute.
3. [02-reviewing-results.md](02-reviewing-results.md) — pull the
   output artifact, validate the provenance chain.
4. [03-diagnosing-failures.md](03-diagnosing-failures.md) — when a
   Run ends in ``failed``, know what to look at.
5. [04-reproducing.md](04-reproducing.md) — export to HPC, re-ingest
   the result, share the bundle with a colleague.

## Current drafting status (T15)

This tutorial is the **Phase-2 draft**, originally authored against
the shipped code 2026-04-23 and refreshed 2026-04-25 against the
post-audit codebase. Phase-3 validation (the release gate) needs a
named scientist to attempt the tutorial cold and time every place
they got stuck. That validation has not happened yet — T15 AC2
remains open pending a real scientist session.

If you're running this tutorial and any step is wrong or unclear,
please open an issue with the chapter + line number. The tutorial
will keep drifting as the underlying code evolves; the repo's
scientists are the authoritative feedback channel.

### What changed in the 2026-04-25 refresh

The tutorial was reconciled against the cluster A–M behavior
changes documented in
[`docs/codebase_audit_2026_04_24.md`](../codebase_audit_2026_04_24.md).
User-visible drift was concentrated in three places:

- **Chapter 00 §6** — the MCP server now eagerly hits the Control
  Plane's ``/healthz`` at startup and exits 2 if the CP is
  unreachable. New ``APECX_MCP_SKIP_HEALTHCHECK=1`` escape hatch
  for offline development. (Audit §3.2.)
- **Chapter 03 §4** (new) and **chapter 04 §1+§2** — the PBS
  bundle's ``run.sh`` is a Phase-2 scaffold that doesn't execute
  the workflow yet (T05 still pending). The stub now writes
  ``stub_completed`` instead of ``completed``, and the ingest path
  flips the Run to ``failed`` with reason ``stub_bundle_detected``.
  Operators round-tripping a stub bundle were previously seeing
  fake green completions; they now see the failure mode for what
  it is. (Audit §3.5.)
- **Chapter 04 §"Reproducibility guarantees"** — fixture count
  bumped from 3 to 10 (7 non-virus fixtures added in the
  previous session's G7 closeout). The harness auto-skips
  live-LLM fixtures unless ``APECX_T12_RUN_LIVE_LLM=1``.

Chapters 01 and 02 are unchanged behavior-wise. Internal
refactors (composer parser robustness, conditional UPDATE on
ingest race, FastAPI session restructure to avoid holding
sessions across ``await``) are not user-visible at the API
surface this tutorial drives.
