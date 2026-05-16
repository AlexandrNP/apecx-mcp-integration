"""Integration tests for benchmark_integrated_similarity + benchmark_max_power.

Pins the cascade for both Item-3 workflows end-to-end with stubbed LLM
and stubbed embedding model. Covers:

* Workflows load via Workflow.from_config (DAG validation + integrity).
* Cascade drains in bounded time (auto_transfer:true on every link).
* Stubbed similarity-search returns the planted cached entry.
* Memory_recorder writes (record_only_if_pass=true gate honored).

Hermetic: no Ollama, no real embedding model loaded.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WF_SIMILARITY = (
    REPO_ROOT
    / "src/apecx_integration/composition/workflows/benchmark_integrated_similarity/workflow.yml"
)
WF_MAX_POWER = (
    REPO_ROOT / "src/apecx_integration/composition/workflows/benchmark_max_power/workflow.yml"
)

_STUB_CODE = (
    "```python\n"
    "from nanobrain.core.step import BaseStep, StepConfig\n"
    "from pydantic import ConfigDict\n"
    "class MyStepConfig(StepConfig):\n"
    "    model_config = ConfigDict(extra='forbid')\n"
    "class MyStep(BaseStep):\n"
    "    COMPONENT_TYPE = 'my_step'\n"
    "    @classmethod\n"
    "    def _get_config_class(cls): return MyStepConfig\n"
    "    async def process(self, input_data, **kw): return input_data\n"
    "```"
)


class _StubLLM:
    @staticmethod
    def invoke(_messages):
        class _R:
            content = _STUB_CODE

        return _R()


def _llm_factory(*_a, **_k):
    return _StubLLM()


class _FakeSTModel:
    """Returns a deterministic 2D vector per text so similarity ranking
    is predictable in tests."""

    def encode(self, texts, **kw):
        import numpy as np

        arr = []
        for t in texts:
            tl = t.lower()
            if "basestep" in tl or "step" in tl:
                arr.append([1.0, 0.0])
            elif "tool" in tl or "calculator" in tl:
                arr.append([0.0, 1.0])
            else:
                arr.append([0.5, 0.5])
        v = np.array(arr, dtype="float32")
        norms = np.linalg.norm(v, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return v / norms


def _patch_st_model(monkeypatch):
    # The SolutionMemoryStep instantiates SentenceTransformer in its
    # _load_embedding_model. Replace that loader to return the fake.
    import apecx_integration.composition.steps.solution_memory_step as smod

    def _stub_load(self):
        if self._embedding_model is None:
            self._embedding_model = _FakeSTModel()
        return self._embedding_model

    monkeypatch.setattr(smod.SolutionMemoryStep, "_load_embedding_model", _stub_load)


def _run_workflow_once(wf_yml: Path, tmp_store: Path, code_spec: str, entry_point: str):
    """Drive the workflow with stubbed LLM (+ stubbed embedding via monkeypatch)
    and return (workflow_output_dict, recorder_status_dict)."""

    # Patch BOTH memory_reader + memory_recorder configs to use tmp_store.
    reader_yml = wf_yml.parent / "steps" / "memory_reader.yml"
    recorder_yml = wf_yml.parent / "steps" / "memory_recorder.yml"
    orig_reader = reader_yml.read_text()
    orig_recorder = recorder_yml.read_text()
    reader_yml.write_text(orig_reader + f'store_path: "{tmp_store}"\n')
    recorder_yml.write_text(orig_recorder + f'store_path: "{tmp_store}"\n')

    try:
        from nanobrain.core.workflow import Workflow

        async def _drive():
            wf = Workflow.from_config(str(wf_yml))
            init = await wf.process(
                {"router_input": {"code_spec": code_spec, "entry_point": entry_point}}
            )
            assert init.get("status") == "data_flow_initiated"
            drained = await wf.wait_for_cascade(timeout=30.0, settle_ms=200)
            assert drained, f"cascade failed to drain for {wf_yml.name}"
            agg = wf.child_steps["aggregator"]
            rec = wf.child_steps["memory_recorder"]
            wo = await agg.step_output_data_units["aggregator_output"].get()
            ws = await rec.step_output_data_units["memory_recorder_output"].get()
            return wo, ws

        return asyncio.run(_drive())
    finally:
        reader_yml.write_text(orig_reader)
        recorder_yml.write_text(orig_recorder)


def test_integrated_similarity_first_pass_empty_memory(tmp_path, monkeypatch):
    """First pass: empty memory -> tier-2 falls back to tier-1 miss; drafter
    runs uncached; recorder writes the AST-passing output."""
    if not WF_SIMILARITY.is_file():
        pytest.skip(f"missing {WF_SIMILARITY}")

    _patch_st_model(monkeypatch)
    tmp_store = tmp_path / "store.json"

    with patch(
        "apecx_integration.composition.steps.benchmark_drafter_step.build_chat_llm",
        _llm_factory,
    ):
        wo, ws = _run_workflow_once(
            WF_SIMILARITY, tmp_store, "Write a BaseStep subclass.", "MyStep"
        )

    assert isinstance(wo, dict)
    assert "class MyStep" in (wo.get("code_source") or "")
    assert wo.get("voted_passes") >= 1
    # Recorder wrote (gate open because aggregator passed).
    assert ws["recorded"] is True
    # On-disk store has the entry.
    assert tmp_store.is_file()
    store = json.loads(tmp_store.read_text())
    assert any(codes for codes in store.values())


def test_integrated_similarity_second_pass_hits_memory(tmp_path, monkeypatch):
    """Two-pass: pre-populate memory; second pass's similarity_read should
    surface the cached entry into the drafter's code_spec."""
    if not WF_SIMILARITY.is_file():
        pytest.skip(f"missing {WF_SIMILARITY}")

    _patch_st_model(monkeypatch)
    tmp_store = tmp_path / "store.json"
    # Pre-populate the store with a high-similarity entry for "step" category.
    tmp_store.write_text(
        json.dumps({"step": ["class CachedStep:\n    pass  # planted from a prior pass"]})
    )

    # Verify the memory_reader's enriched spec hits the planted entry by
    # inspecting drafter's INPUT after the cascade drained.
    with patch(
        "apecx_integration.composition.steps.benchmark_drafter_step.build_chat_llm",
        _llm_factory,
    ):
        reader_yml = WF_SIMILARITY.parent / "steps" / "memory_reader.yml"
        recorder_yml = WF_SIMILARITY.parent / "steps" / "memory_recorder.yml"
        orig_reader = reader_yml.read_text()
        orig_recorder = recorder_yml.read_text()
        reader_yml.write_text(orig_reader + f'store_path: "{tmp_store}"\n')
        recorder_yml.write_text(orig_recorder + f'store_path: "{tmp_store}"\n')

        try:
            from nanobrain.core.workflow import Workflow

            async def _drive():
                wf = Workflow.from_config(str(WF_SIMILARITY))
                await wf.process(
                    {
                        "router_input": {
                            "code_spec": "Write a BaseStep subclass.",
                            "entry_point": "MyStep",
                        }
                    }
                )
                drained = await wf.wait_for_cascade(timeout=30.0, settle_ms=200)
                assert drained
                # Inspect memory_reader's output to confirm enrichment.
                reader = wf.child_steps["memory_reader"]
                reader_out = await reader.step_output_data_units["memory_reader_output"].get()
                return reader_out

            reader_out = asyncio.run(_drive())
        finally:
            reader_yml.write_text(orig_reader)
            recorder_yml.write_text(orig_recorder)

    assert reader_out["memory_hit"] is True
    assert reader_out["memory_mode"] == "similarity"
    assert "CachedStep" in reader_out["code_spec"]


def test_max_power_cascade_drains(tmp_path, monkeypatch):
    """Max-power composition: 5-node cascade w/ tier-2 memory + perturbing
    drafter + AST voter + recorder. Just confirms the cascade drains and
    every link transfers (auto_transfer regression catch)."""
    if not WF_MAX_POWER.is_file():
        pytest.skip(f"missing {WF_MAX_POWER}")

    _patch_st_model(monkeypatch)
    tmp_store = tmp_path / "store.json"

    with patch(
        "apecx_integration.composition.steps.prompt_perturbing_drafter_step.build_chat_llm",
        _llm_factory,
    ):
        wo, ws = _run_workflow_once(WF_MAX_POWER, tmp_store, "Write a BaseStep subclass.", "MyStep")

    assert isinstance(wo, dict)
    assert "class MyStep" in (wo.get("code_source") or "")
    assert wo.get("n_samples") == 3  # perturbing drafter emits 3
    assert ws["recorded"] is True
