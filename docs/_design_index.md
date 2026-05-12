# docs/ — what's here, one line each

Navigation aid. For deeper context: `README.md` (user-facing),
`CLAUDE.md` (LLM-facing repo rules).

## User-facing

| Doc | What it is |
|---|---|
| `architecture.md` | **Canonical** end-to-end map (Mermaid diagrams, MCP tools, ontologies, invocation paths, test surface). Start here for any architecture question. |
| `mcp_integration.md` | Operator install + per-tool reference + troubleshooting. |
| `QUICKSTART.md` | Fresh-laptop walkthrough (referenced from README). |
| `tutorial/` | Multi-chapter walkthrough from install to reproducible run. |
| `api_contract.yaml` | HTTP API contract for the control-plane backend. |

## Design contracts (operational — cited from source docstrings)

| Doc | What it is |
|---|---|
| `CONTRACTS.md` | Every design contract cited from `nanobrain` and `apecx-mcp-integration` Python source docstrings. Anchored sections (`#g7`, `#decision-p6+a`, `#td-vocab`, etc.) are STABLE. |
| `whitelist_layering.md` | G36 two-stage defense (AST scanner + YAML class-path whitelist) — referenced from `composition/sandbox.py` and `nanobrain/core/import_whitelist.py`. |

## Operational / status tracking

| Doc | What it is |
|---|---|
| `WORKAROUND_INVENTORY.md` | Active workarounds awaiting framework fixes; updated per ship. |
| `implementation_task_graph.md` | 165 file-level tasks across 4 tracks with stable IDs. Cite the task ID in commits. |
| `supervisor_handbook.md` | Knowledge-transfer artifact for new external supervisors of the apecx composer: scope, day-one checklist, drift patterns D1-D8, gates/rules currently shipped, signals to monitor, distillation policy. Pinned by `tests/unit/test_supervisor_handbook_pinned.py`. |

## Assets

`figures/` — diagrams referenced by `architecture.md`.
`architecture_slides.pptx` — accompanying slides.

## Cleanup history

**2026-05-11** (this chain): replaced 16 multi-purpose design docs
with one consolidated `CONTRACTS.md` (anchored per docstring
citation). Deleted: `agent_communication_protocol.md`,
`agent_workflow_authoring.md`, `autonomous_workflow_agent.md`,
`data_layer_evolution.md`, `development_roadmap.md`,
`external_tool_integration.md`, `hitl_safety_gates.md`,
`hpc_reproducibility_spec.md`, `llm_prompt_contracts.md`,
`meta_workflow_orchestration.md`, `nanobrain_alignment_audit.md`,
`nanobrain_capability_gaps.md`, `reasoning_patterns_library.md`,
`security_threat_model.md`, `tool_descriptor_contract.md`,
`workflow_output_contract.md`. All cited anchors preserved in
`CONTRACTS.md`. The 104 docstring references across both repos were
rewritten in lockstep — see git log for the migration commit.

**2026-05-11** (earlier): removed `deployment_architecture.md`,
`mcp_surface.md`, `multiagent_architecture.md`,
`nanobrain_workflow_design.md`, `violin_bvbrc_workflow.md` (zero
workspace-wide references outside docs/).
