# Deployment E2E — findings

What a REAL clean deploy + real tool/workflow runs surfaced (2026-07-02), each with the evidence that
proves it. Produced by building `tests/e2e_deploy/` and running it under a FRESH venv install of the
integration branch (all four workstreams merged). Context: the prior "deployment doc" was a prose
runbook whose concrete examples were written from memory, never a real call — this is the correction.

Legend: **FIXED** (corrected + pinned by a test) · **DOC** (fixed in the doc/rule, not code) ·
**GATED** (real issue logged for an owner decision, deliberately not patched).

---

## F1 — the shared dev `.venv` editable-installs the WRONG worktree — DOC (hygiene)
`apecx-mcp-integration/.venv` resolves `apecx_integration` to
`…/wt-workflow-crafting/src` — an unrelated, stale branch (`external-orchestration-reuse-plan`), not
`main` and not any of the four workstream branches. So the "installed" `apecx-mcp` / `apecx-setup` on
the dev box run code that is neither shipped nor under test.
**Evidence:** `apecx-mcp-integration/.venv/bin/python -c "import apecx_integration; print(__file__)"` →
`…/wt-workflow-crafting/…`. **Refuted for packaging:** a fresh `pip install .` resolves
`apecx_integration` to `fresh-venv/lib/python3.12/site-packages/…` (correct). So this is a dev-venv
HYGIENE hazard, not a build defect. **Rule (added to the doc):** a deploy test MUST use a fresh venv;
never reuse the dev `.venv`.

## F2 — every concrete tool-usage example in the runbook was wrong — FIXED
`harmonized_search` requires `term` **and** `index` (a valid index, e.g. `bvbrc_genome`), not `query`;
`describe_workflow` requires `name`, not `workflow_name`. The runbook documented the non-existent
params. Calling the real tool with them raises a pydantic "field required" ToolError.
**Evidence + pin:** `test_deploy_tool_usage.py::{test_harmonized_search_requires_term_and_index,
test_harmonized_search_rejects_the_runbook_signature, test_describe_workflow_uses_name_param}`.

## F3 — all session-long "verified against real backends" ran on a hand-provisioned dev box — FIXED
`apecx-setup verify` on the dev box shows dict (735 MB), VIOLIN, FAISS, rhea, host-ollama+model all
already present; a NEW environment builds NONE of that. No prior run stood the system up clean.
**Addressed:** the harness now runs against a FRESH venv install; the clean-install + boot path is
exercised (F1 refutation) and the tool/workflow runs are real.

## F4 — GNU coreutils (`timeout`) not on macOS — DOC (portability note)
`timeout` is absent on macOS (the documented target). It bit the verification COMMANDS during this work
(`timeout 90 apecx-setup verify` → `command not found: timeout`). **Evidence:** the failure above.
**Rule (added to the doc):** verification/CI commands must not assume GNU coreutils; use in-process
timeouts (the harness uses `subprocess.run(..., timeout=)`, not shell `timeout`).

## F5 — no single checkout had all four workstreams — FIXED
The four workstreams live on 3 unmerged stacked branches; the doc's "integrated system" existed nowhere.
`#1c` is off `main` (not off `#7` as an earlier note claimed — merge-base is `095ed06`).
**Fixed:** created `integration-deploy-e2e` (start at `infra-dashboard` which already contains `#7`+W3,
then one clean merge of `#1c` → merge commit `9acc4aa`). Full unit suite **2204 passed / 0 failures** on
the integrated tree; `build_server().list_tools()` = 17, `reload_backend` (W3) + `SandboxedNovelStep`
(#1c) both present.

## F6 — viral_epitope_analysis: E1 query retrieved E2-heavy evidence — GATED (correctness, investigate)
Querying "conserved epitopes on chikungunya virus **E1** glycoprotein" returned a completed report whose
retrieved publications are largely about **E2**. Possibly acceptable (alphavirus E1/E2 cross-reference)
or a real relevance gap — a status check would never catch it. **Not patched** (needs a domain call);
logged for investigation. The harness asserts the report is substantial + on-topic (mentions the virus),
not the E1-vs-E2 precision.

## F7 — `apecx-mcp --help` epilog + CLAUDE.md hardcode "15 tools"; reality is 17 — DOC (minor)
The two epitope-assessment workflows (#2) make it 17. The count drifts (CLAUDE.md itself warns three
sources disagreed); the harness pins tool PRESENCE, not the count. **Fixed** in the doc by deriving the
list from `list_tools()` rather than a hardcoded number.

## F8 — the discovery surface is disconnected from the runnable tool surface — GATED (UX)
`run_workflow("viral_epitope_analysis")` runs the flagship successfully AND `viral_epitope_analysis` is a
registered tool — but `list_workflows` (which returns only the 4 composer-catalog workflows) does NOT
list it, and `describe_workflow("viral_epitope_analysis")` → `{"error": "unknown workflow"}`. Meanwhile
`apecx_capabilities` tells the model to "discover names with `list_workflows`". So the self-documentation
points at a discovery tool that can't see the flagship. The discovery set is also config-dependent
(differs dev vs installed). **Real UX/discoverability gap** requiring an owner decision (unify the
discovery + tool surfaces, or make `apecx_capabilities` name the promoted tools too). **Not patched**;
current behavior PINNED by `test_deploy_tool_surface.py::test_discovery_and_tool_surface_are_disconnected_F8`.

## F9 — the `run_workflow` TOOL returns the report as TEXT, not a `{status, markdown}` envelope — encoded
The internal `run_workflow(name, params)` function returns `{status, markdown, run_id, …}`; the REGISTERED
tool (real MCP dispatch) returns the finished report as desktop-presentation TEXT (a content block,
prefixed "INSTRUCTIONS FOR THE ASSISTANT PRESENTING THIS RESULT…"). A client sees the report, not a
status dict. **Encoded** in the harness (success = a substantial on-topic report, per G127 assert-on-value);
documented as the MCP-path contract so a future test/consumer doesn't assert on the wrong shape.

---

## What the harness proves (positive)
- The deployment artifact INSTALLS cleanly into a fresh venv (nanobrain + apecx-harvesters pulled from
  their pinned git refs) and runs from site-packages.
- The real scientist tool surface (17 tools) boots and every scientist-facing tool is registered.
- Real tool calls with the correct signatures return real content (dict-resolved Globus search, workflow
  schemas).
- TWO real workflows (`rag_e2e_synthesis`, `viral_epitope_analysis`) run end-to-end against a live LLM and
  produce substantial, on-topic reports.

Run it: `pytest tests/e2e_deploy/` (auto-skips the docker/ollama-gated checks on a bare box).
