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

This tutorial is the **Phase-2 draft** authored against the shipped
code 2026-04-23. Phase-3 validation (the release gate) needs a
named scientist to attempt the tutorial cold and time every place
they got stuck. That validation has not happened yet — T15 AC2
remains open pending a real scientist session.

If you're running this tutorial and any step is wrong or unclear,
please open an issue with the chapter + line number. The tutorial
will keep drifting as the underlying code evolves; the repo's
scientists are the authoritative feedback channel.
