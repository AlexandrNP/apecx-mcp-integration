"""Integration tests for the web-search codegen composition.

Three surfaces, all UNCONDITIONAL (no network — a fake search backend
is injected for the cascade test):

1. **Workflow-load tests** — benchmark_max_power_websearch (YAML) +
   benchmark_ablation_websearch_only (YAML) + the lightweight-builder
   variant all load + validate.
2. **Fake-backend cascade test** — a minimal
   ``workflow_input -> web_search_context -> workflow_output`` workflow
   is driven end-to-end with a fake search backend, proving the
   WebSearchContextStep works inside a real trigger cascade (envelope
   unwrap + tool call + code_spec enrichment + output transfer). No
   network, no LLM.

The full benchmark_max_power_websearch workflow is NOT cascaded here —
it contains LLM drafter steps. The cascade test isolates the ONE node
this chain adds (web_search_context); the LLM steps are exercised by
the ablation sweeps (D1/D2).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_WF_DIR = REPO_ROOT / "src/apecx_integration/composition/workflows"
MAX_POWER_WS_YML = _WF_DIR / "benchmark_max_power_websearch/workflow.yml"
ABLATION_WS_YML = _WF_DIR / "benchmark_ablation_websearch_only/workflow.yml"


# ---- 1. Workflow-load tests (unconditional) ----


def test_max_power_websearch_yaml_loads():
    from nanobrain.core.workflow import Workflow

    wf = Workflow.from_config(str(MAX_POWER_WS_YML))
    assert wf.name == "benchmark_max_power_websearch"
    assert "web_search_context" in wf.child_steps
    # max_power's 5 steps + the 1 new web_search_context node.
    assert len(wf.child_steps) == 6
    assert len(wf.step_links) == 8


def test_ablation_websearch_only_yaml_loads():
    from nanobrain.core.workflow import Workflow

    wf = Workflow.from_config(str(ABLATION_WS_YML))
    assert wf.name == "benchmark_ablation_websearch_only"
    # F17's 2 steps (router, drafter) + the 1 new web_search_context node.
    assert sorted(wf.child_steps.keys()) == [
        "drafter",
        "task_router",
        "web_search_context",
    ]
    assert len(wf.step_links) == 4


def test_max_power_websearch_lightweight_builder_parity():
    from nanobrain.core.workflow import Workflow

    from apecx_integration.composition.workflows.benchmark_max_power_websearch_lightweight_builder import (  # noqa: E501
        build_max_power_websearch_workflow_lightweight,
    )

    yaml_wf = Workflow.from_config(str(MAX_POWER_WS_YML))
    lw_wf = build_max_power_websearch_workflow_lightweight()
    # Topology parity: same step ids, same link count.
    assert sorted(yaml_wf.child_steps.keys()) == sorted(lw_wf.child_steps.keys())
    assert len(yaml_wf.step_links) == len(lw_wf.step_links) == 8


# ---- 2. Fake-backend cascade test (unconditional) ----


def _stage_minimal_websearch_workflow(tmp_path: Path) -> Path:
    """Write a minimal input -> web_search_context -> output workflow.

    All three configs land in one dir so the step's relative
    ``web_search_tool_config`` reference resolves cleanly.
    """
    steps_dir = tmp_path / "steps"
    steps_dir.mkdir()

    (steps_dir / "web_search_tool.yml").write_text(
        "name: web_search_test\n"
        "tool_type: external\n"
        "description: test web search\n"
        "parameters:\n"
        "  backend: duckduckgo\n"
        "  max_results: 3\n"
        "tool_card:\n"
        "  capabilities: ['web_search']\n"
    )
    (steps_dir / "web_search_context.yml").write_text(
        "class: 'apecx_integration.composition.steps.web_search_context_step.WebSearchContextStep'\n"
        "name: web_search_context\n"
        "description: 'test websearch node'\n"
        "web_search_tool_config: 'web_search_tool.yml'\n"
        "max_results: 3\n"
        "input_data_units:\n"
        "  web_search_context_input:\n"
        "    class: 'nanobrain.core.data_unit.DataUnitMemory'\n"
        "    name: web_search_context_input\n"
        "    persistent: false\n"
        "output_data_units:\n"
        "  web_search_context_output:\n"
        "    class: 'nanobrain.core.data_unit.DataUnitMemory'\n"
        "    name: web_search_context_output\n"
        "    persistent: false\n"
        "triggers:\n"
        "  - class: 'nanobrain.core.trigger.DataUnitChangeTrigger'\n"
        "    data_unit: 'web_search_context_input'\n"
    )
    wf_yml = tmp_path / "workflow.yml"
    wf_yml.write_text(
        "name: websearch_cascade_test\n"
        "description: 'minimal input -> web_search_context -> output'\n"
        "config_version: 2\n"
        "input_data_units:\n"
        "  workflow_input:\n"
        "    class: 'nanobrain.core.data_unit.DataUnitMemory'\n"
        "    name: workflow_input\n"
        "    persistent: false\n"
        "output_data_units:\n"
        "  workflow_output:\n"
        "    class: 'nanobrain.core.data_unit.DataUnitMemory'\n"
        "    name: workflow_output\n"
        "    persistent: false\n"
        "steps:\n"
        "  web_search_context:\n"
        "    class: 'apecx_integration.composition.steps.web_search_context_step.WebSearchContextStep'\n"
        "    config: 'steps/web_search_context.yml'\n"
        "links:\n"
        "  input_to_ws:\n"
        "    class: 'nanobrain.core.link.DirectLink'\n"
        "    config:\n"
        "      link_type: direct\n"
        "      source: workflow_input\n"
        "      target: web_search_context.web_search_context_input\n"
        "      auto_transfer: true\n"
        "  ws_to_output:\n"
        "    class: 'nanobrain.core.link.DirectLink'\n"
        "    config:\n"
        "      link_type: direct\n"
        "      source: web_search_context.web_search_context_output\n"
        "      target: workflow_output\n"
        "      auto_transfer: true\n"
    )
    return wf_yml


def test_websearch_node_cascade_with_fake_backend(tmp_path):
    """Drive a real Workflow cascade through web_search_context with a
    fake backend — proves envelope unwrap + tool call + code_spec
    enrichment + output transfer. No network."""
    from nanobrain.core.workflow import Workflow
    from nanobrain.library.tools.web_search import WebSearchBackend

    class _FakeBackend(WebSearchBackend):
        name = "fake"

        def __init__(self):
            self.calls: list[tuple[str, int]] = []

        async def search(self, query, *, max_results):
            self.calls.append((query, max_results))
            return [
                {
                    "title": "Reverse complement — Wikipedia",
                    "url": "http://x/rc",
                    "snippet": "the reverse complement of a DNA sequence",
                },
            ]

    wf_yml = _stage_minimal_websearch_workflow(tmp_path)

    async def _drive():
        wf = Workflow.from_config(str(wf_yml))
        # Swap the real DDG backend for the fake one (no network).
        fake = _FakeBackend()
        wf.child_steps["web_search_context"]._tool.backend = fake

        init = await wf.process(
            {
                "web_search_context_input": {
                    "code_spec": "Compute the reverse complement of a DNA string",
                    "task_category": "open_rosalind",
                    "entry_point": "solve",
                }
            }
        )
        assert init.get("status") == "data_flow_initiated"
        drained = await wf.wait_for_cascade(timeout=15.0, settle_ms=150)
        assert drained, "websearch cascade failed to drain"
        step = wf.child_steps["web_search_context"]
        out = await step.step_output_data_units["web_search_context_output"].get()
        return out, fake

    out, fake = asyncio.run(_drive())

    # The fake backend was invoked — and EVERY call received the
    # correctly UNWRAPPED query (not the trigger envelope). The cascade
    # may fire the step more than once (known framework trigger quirk);
    # assert per-call shape, not call count.
    assert fake.calls, "fake search backend was never invoked"
    for query, max_results in fake.calls:
        # The query is derived from code_spec's first line.
        assert "reverse complement" in query.lower()
        assert max_results == 3

    # The step enriched code_spec and the output carried through the
    # cascade to the workflow output.
    assert isinstance(out, dict)
    assert out["websearch_hit"] is True
    assert out["websearch_results_used"] == 1
    assert "Relevant web context" in out["code_spec"]
    assert "Reverse complement — Wikipedia" in out["code_spec"]
    # Original spec preserved + passthrough field carried.
    assert "Compute the reverse complement" in out["code_spec"]
    assert out["entry_point"] == "solve"


def test_websearch_node_cascade_backend_failure_surfaces(tmp_path):
    """A backend failure inside the cascade must NOT silently vanish —
    the step raises, and the failure is observable (not a green run
    with empty context)."""
    from nanobrain.core.workflow import Workflow
    from nanobrain.library.tools.web_search import WebSearchBackend

    class _FailingBackend(WebSearchBackend):
        name = "failing"

        async def search(self, query, *, max_results):
            raise RuntimeError("simulated rate-limit rejection")

    wf_yml = _stage_minimal_websearch_workflow(tmp_path)

    async def _drive():
        wf = Workflow.from_config(str(wf_yml))
        wf.child_steps["web_search_context"]._tool.backend = _FailingBackend()
        await wf.process(
            {
                "web_search_context_input": {
                    "code_spec": "anything",
                    "task_category": "x",
                }
            }
        )
        await wf.wait_for_cascade(timeout=10.0, settle_ms=150)
        step = wf.child_steps["web_search_context"]
        # The step raised inside process(); its output DU was never
        # written. The failure is observable as "no output", NOT as a
        # green run with empty context.
        return await step.step_output_data_units["web_search_context_output"].get()

    out = asyncio.run(_drive())
    # The output DU is empty/None because the step raised — the failure
    # did not silently degrade to "no context".
    assert out is None or out == {} or out == ""
