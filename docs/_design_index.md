# docs/ — what's here, one line each

Navigation aid for the `docs/` directory. For deeper context, see
`README.md` (user-facing), `CLAUDE.md` (LLM-facing repo rules), and
the individual docs.

## Current-state references

| Doc | What it is |
|---|---|
| `architecture.md` | **Canonical** end-to-end map (Mermaid diagrams, MCP tools, ontologies, invocation paths, test surface). Start here for any architecture question. |
| `mcp_integration.md` | Operator install + per-tool reference + troubleshooting. |
| `QUICKSTART.md` | Fresh-laptop walkthrough (referenced from README). |
| `tutorial/` | Multi-chapter walkthrough from install to reproducible run. |
| `api_contract.yaml` | HTTP API contract for the control-plane backend. |

## Operational references

| Doc | What it is |
|---|---|
| `WORKAROUND_INVENTORY.md` | Active workarounds awaiting framework fixes; updated per ship. |
| `nanobrain_capability_gaps.md` | G1–G45 framework gap proposals (most shipped; rest paired with WORKAROUND_INVENTORY). |
| `implementation_task_graph.md` | 165 file-level tasks across 4 tracks with stable IDs. Cite the ID in commits. |
| `development_roadmap.md` | 5-phase delivery plan + open questions. |
| `whitelist_layering.md` | G36 two-stage defense (AST scanner + YAML class-path whitelist). |

## Design references (load-bearing — cited from nanobrain source docstrings)

| Doc | Cited by |
|---|---|
| `workflow_output_contract.md` | `nanobrain/library/orchestration/execution_plan.py` |
| `reasoning_patterns_library.md` | `nanobrain/library/steps/loop_controller.py` |
| `data_layer_evolution.md` | `nanobrain/library/runtime/data_source_registry.py` |
| `tool_descriptor_contract.md` | UTD spec — cross-cited from multiple design docs. |

## Design references (maintainer reference; cross-cited within docs/)

`agent_workflow_authoring.md`, `agent_communication_protocol.md`,
`autonomous_workflow_agent.md`, `external_tool_integration.md`,
`hitl_safety_gates.md`, `hpc_reproducibility_spec.md`,
`llm_prompt_contracts.md`, `meta_workflow_orchestration.md`,
`nanobrain_alignment_audit.md`, `security_threat_model.md`.

Each carries a stable section anchor (e.g., `§3.2`, `P7`) so source
code can cite specific contracts without depending on the full prose.

## Figures

`figures/` — diagrams referenced by `architecture.md`.

## Cleanup history

2026-05-11: removed `deployment_architecture.md`, `mcp_surface.md`,
`multiagent_architecture.md`, `nanobrain_workflow_design.md`,
`violin_bvbrc_workflow.md` (zero workspace-wide references outside
docs/). If your workflow depended on one of these, the work it
described is either shipped (see git log) or merged into another doc.
