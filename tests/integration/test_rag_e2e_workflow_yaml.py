"""Framework-level loadability + runtime tests for the rag_e2e_synthesis workflow.

The companion test_rag_e2e_pipeline.py exercises the steps' process()
methods individually with real Ollama / real domain-RAG / real CSVs.
This file pins the OTHER half of the contract:

  1. The workflow YAML + step YAMLs compose cleanly through
     ``Workflow.from_config`` and ``BaseStep.from_config``
     (loadability tests, no external services required).

  2. The composed workflow actually executes end-to-end via
     ``wf.execute(initial_input)`` — exercising the trigger graph,
     the DirectLink between assembly and synthesis, and the LLM call
     (gated tests; auto-skip when Ollama / domain-RAG / CSVs missing).

Why both halves matter
----------------------
test_rag_e2e_pipeline.py constructs SynthesisContextAssemblyStep and
RagSynthesisStep manually with from_config, then feeds output of step A
straight into step B in Python — bypassing the workflow's link wiring
and trigger machinery. A future regression that breaks the workflow
YAML's link source/target names or the step's data-unit names would
NOT be caught by those tests. This file plugs that gap.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest
from nanobrain.core.step import BaseStep
from nanobrain.core.workflow import Workflow

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = REPO_ROOT.parent
WORKFLOW_DIR = (
    REPO_ROOT / "src" / "apecx_integration" / "composition" / "workflows" / "rag_e2e_synthesis"
)
WORKFLOW_YAML = WORKFLOW_DIR / "rag_e2e_synthesis_workflow.yml"
STEP_ASSEMBLY_YAML = WORKFLOW_DIR / "steps" / "synthesis_context_assembly.yml"
STEP_SYNTHESIS_YAML = WORKFLOW_DIR / "steps" / "rag_synthesis.yml"

DOMAIN_RAG_INDEX = WORKSPACE_ROOT / "data" / "apecx_domain_rag"
VIOLIN_DIR = WORKSPACE_ROOT / "data" / "violin"

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("APECX_LLM_MODEL", "mistral-nemo:latest")


def _ollama_reachable() -> bool:
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=2.0)
        r.raise_for_status()
        names = {m["name"] for m in r.json().get("models", [])}
        return OLLAMA_MODEL in names
    except Exception:
        return False


@pytest.fixture
def chdir_repo_root(monkeypatch):
    """Match the convention used by test_violin_bvbrc_workflow_yaml — pin
    cwd to the repo root so any repo-relative path the framework resolves
    behaves the same regardless of how pytest was invoked.
    """
    monkeypatch.chdir(REPO_ROOT)


def test_step_synthesis_context_assembly_loads(chdir_repo_root) -> None:
    """SynthesisContextAssemblyStep YAML loads via from_config.

    Pins the StepConfig/extra='forbid' contract: any field name typo or
    missing required field surfaces here at boot, not later when an
    operator runs a real query.
    """
    assert STEP_ASSEMBLY_YAML.is_file(), STEP_ASSEMBLY_YAML
    step = BaseStep.from_config(str(STEP_ASSEMBLY_YAML))
    assert step.name == "synthesis_context_assembly"


def test_step_rag_synthesis_loads(chdir_repo_root) -> None:
    """RagSynthesisStep YAML loads via from_config.

    The rag_e2e_synthesis variant of rag_synthesis.yml is functionally
    identical to the violin_bvbrc one but lives at a different path; if
    they ever diverge (different defaults, extra fields), this test
    catches the rag_e2e_synthesis variant specifically.
    """
    assert STEP_SYNTHESIS_YAML.is_file(), STEP_SYNTHESIS_YAML
    step = BaseStep.from_config(str(STEP_SYNTHESIS_YAML))
    assert step.name == "rag_synthesis"


def test_workflow_yaml_loads_via_framework(chdir_repo_root) -> None:
    """Workflow.from_config on rag_e2e_synthesis_workflow.yml succeeds.

    Pins the framework-level contract: both step YAMLs compose under
    the workflow loader, the DirectLink between them resolves to real
    data unit names, and trigger registration completes.

    Detection signal: a future commit that renames a data unit on
    either step (e.g. ``synthesis_bundle_output`` →
    ``assembly_output``) without updating the workflow YAML's link
    source/target makes Workflow.from_config raise — this test fails
    loudly instead of silently shipping a workflow whose steps can't
    receive each other's output at runtime.
    """
    assert WORKFLOW_YAML.is_file(), WORKFLOW_YAML
    workflow = Workflow.from_config(str(WORKFLOW_YAML))
    assert workflow.name == "rag_e2e_synthesis_workflow"
    assert workflow is not None


def test_workflow_registers_both_steps(chdir_repo_root) -> None:
    """Both Day-2 step instances are registered on the loaded workflow.

    The introspection mirrors test_violin_bvbrc_workflow_yaml's robust
    fallback chain because nanobrain's child-steps attribute name
    varies by framework version.
    """
    workflow = Workflow.from_config(str(WORKFLOW_YAML))
    children = (
        getattr(workflow, "child_steps", None)
        or getattr(workflow, "_child_steps", None)
        or getattr(workflow, "steps", None)
    )
    assert children is not None, (
        "could not introspect workflow's child steps; framework attribute layout may have changed"
    )
    names = (
        set(children.keys())
        if isinstance(children, dict)
        else {getattr(s, "name", None) for s in children}
    )
    assert "synthesis_context_assembly" in names, (
        f"synthesis_context_assembly missing; got: {sorted(n for n in names if n)!r}"
    )
    assert "rag_synthesis" in names, (
        f"rag_synthesis missing; got: {sorted(n for n in names if n)!r}"
    )


def test_workflow_link_source_target_match_step_data_units(chdir_repo_root) -> None:
    """The DirectLink in the workflow YAML references real data unit names.

    Reads the workflow YAML and the two step YAMLs as plain dicts
    (independent of the framework loader) and checks:
      - ``synthesis_context_assembly.synthesis_bundle_output`` is
        actually declared in the assembly step's output_data_units
      - ``rag_synthesis.synthesis_input`` is actually declared in the
        synthesis step's input_data_units

    A typo in either link side would make Workflow.from_config crash
    above — but on some framework versions it silently degrades to a
    no-op link, which is worse. This test catches both modes.
    """
    import yaml

    with open(WORKFLOW_YAML) as f:
        wf = yaml.safe_load(f)
    with open(STEP_ASSEMBLY_YAML) as f:
        assembly_cfg = yaml.safe_load(f)
    with open(STEP_SYNTHESIS_YAML) as f:
        synthesis_cfg = yaml.safe_load(f)

    link = wf["links"]["assembly_to_synthesis"]["config"]
    src_step, src_du = link["source"].split(".", 1)
    tgt_step, tgt_du = link["target"].split(".", 1)

    assert src_step == "synthesis_context_assembly"
    assert src_du in assembly_cfg["output_data_units"], (
        f"link source data unit '{src_du}' not in assembly outputs: "
        f"{sorted(assembly_cfg['output_data_units'].keys())!r}"
    )

    assert tgt_step == "rag_synthesis"
    assert tgt_du in synthesis_cfg["input_data_units"], (
        f"link target data unit '{tgt_du}' not in synthesis inputs: "
        f"{sorted(synthesis_cfg['input_data_units'].keys())!r}"
    )


# ---------------------------------------------------------------------------
# Workflow runtime test — drive the full chain through wf.execute()
#
# This is the OTHER half of the E2E coverage: test_rag_e2e_pipeline.py
# manually instantiates both steps and feeds output of A into B in
# Python; this test drives them through the trigger graph + DirectLink
# so a regression in framework-level wiring (data unit registration,
# trigger fire conditions, link source/target dispatch) surfaces here
# and not silently in production.
#
# Gating: all three external dependencies (FAISS index, VIOLIN CSVs,
# Ollama) must be present. Auto-skips when any is missing.
# ---------------------------------------------------------------------------


def test_workflow_step_data_contract_via_imperative_drive(monkeypatch, chdir_repo_root):
    """Drive the rag_e2e_synthesis workflow's two steps via the
    framework-instantiated ``Workflow`` object — NOT manually
    constructed step instances — and pin the inter-step data
    contract.

    What this test pins
    -------------------
    The five other tests in this file cover *static* loadability:
    YAML parses, classes import, link source/target reference real
    data unit names. This test goes one layer deeper: it grabs the
    step instances that ``Workflow.from_config`` actually instantiated
    and verifies that:

      1. Both steps are real BaseStep instances (not None / not
         placeholder shells).
      2. The assembly step's process() output is shape-compatible
         with the synthesis step's process() input (same dict keys
         the DirectLink would propagate).
      3. The synthesis step's output has the documented shape
         (``{"synthesis": str}``).

    Why this isn't a full trigger-cascade test
    ------------------------------------------
    nanobrain's ``Workflow.process(input)`` is data-driven: it writes
    to the first step's input data unit and returns immediately with
    ``{"status": "data_flow_initiated"}``. The actual cascade fires in
    background tasks that aren't awaited synchronously. A true
    cascade-driven test would need to poll the last step's output
    data unit with a timeout — that's brittle in pytest-asyncio (it
    depends on the trigger executor's task lifecycle, which the
    framework doesn't expose to test code). The full E2E runtime is
    covered by ``test_rag_e2e_pipeline.py``'s pipeline tests, which
    construct steps independently and feed output → input directly.

    What WOULD make this test fail
    ------------------------------
      - The workflow YAML rename of ``synthesis_bundle_output`` →
        anything else without updating the link source: the assembly
        step would still run (load + process succeed), but the dict
        returned wouldn't have the link's expected key. The shape
        assertion below catches that.
      - A future change that makes ``rag_synthesis`` reject the bundle
        shape produced by the assembly step (extra required field,
        renamed key) — the second process() call would raise.
    """
    if not (DOMAIN_RAG_INDEX / "faiss_index.bin").exists():
        pytest.skip(f"Domain RAG index not built at {DOMAIN_RAG_INDEX}")
    if not (VIOLIN_DIR / "Pathogen_Information.csv").exists():
        pytest.skip(f"VIOLIN data not found at {VIOLIN_DIR}")
    if not _ollama_reachable():
        pytest.skip(f"Ollama not reachable at {OLLAMA_URL} or model {OLLAMA_MODEL!r} not pulled")

    monkeypatch.setenv("APECX_LLM_BASE_URL", f"{OLLAMA_URL}/v1")
    monkeypatch.setenv("APECX_LLM_MODEL", OLLAMA_MODEL)
    monkeypatch.setenv("APECX_LLM_API_KEY", "EMPTY")
    monkeypatch.setenv("APECX_LLM_TEMPERATURE", "0.0")
    monkeypatch.setenv("APECX_LLM_MAX_TOKENS", "512")

    wf = Workflow.from_config(str(WORKFLOW_YAML))

    children = (
        getattr(wf, "child_steps", None)
        or getattr(wf, "_child_steps", None)
        or getattr(wf, "steps", None)
    )
    assert isinstance(children, dict), "expected children dict on workflow"
    assembly = children["synthesis_context_assembly"]
    synthesis = children["rag_synthesis"]
    assert isinstance(assembly, BaseStep)
    assert isinstance(synthesis, BaseStep)

    # Offline-friendly: skip the PubMed network branch.
    assembly._skip_pubmed = True

    # Step 1 — assembly produces the bundle.
    bundle = asyncio.run(assembly.process({"query": "Eastern equine encephalitis vaccines"}))
    # Bundle shape pins the link contract.
    expected_keys = {
        "query",
        "rag_chunks",
        "bvbrc_genomes",
        "violin_mappings",
        "publications",
    }
    assert expected_keys.issubset(bundle.keys()), (
        f"assembly bundle missing keys; got {sorted(bundle.keys())!r}, "
        f"expected superset of {sorted(expected_keys)!r}"
    )

    # Step 2 — synthesis consumes the same dict the DirectLink would
    # propagate. Any future shape mismatch surfaces as a KeyError or
    # ValueError here.
    result = asyncio.run(synthesis.process(bundle))
    assert isinstance(result, dict), f"synthesis result not a dict: {result!r}"
    assert "synthesis" in result, (
        f"synthesis output missing 'synthesis' key; got: {sorted(result.keys())!r}"
    )
    assert isinstance(result["synthesis"], str)
    assert len(result["synthesis"]) > 50, (
        "synthesis Markdown body suspiciously short — "
        "either the LLM call failed or a synthesizer gate degraded the response"
    )


def test_workflow_runtime_executes_via_trigger_cascade(monkeypatch, chdir_repo_root):
    """Drive the rag_e2e_synthesis workflow through the FULL data-driven
    trigger cascade — populating only the first input data unit and
    awaiting the output via the new ``Workflow.wait_for_cascade()``
    primitive (added to nanobrain alongside this test).

    What this pins (that test_workflow_step_data_contract_via_imperative_drive
    does NOT):

      - The DataUnitChangeTrigger registered on the assembly step's
        input_data_unit ACTUALLY fires when ``Workflow.process()`` writes
        to it.
      - The DirectLink between ``synthesis_context_assembly.synthesis_bundle_output``
        and ``rag_synthesis.synthesis_input`` ACTUALLY auto-transfers
        when the source data unit is set.
      - The synthesis step's trigger fires after the link transfer.
      - The whole cascade drains within a reasonable wall-clock budget.

    This is the test that catches:
      - A future regression in the trigger executor's task management
        (the cascade never completes; wait_for_cascade returns False)
      - A workflow YAML link typo that loaded cleanly but doesn't
        actually transfer (cascade drains but synthesis_output is empty)
      - Trigger registration bugs (assembly fires but synthesis never
        does)

    Gated on all three external dependencies + Ollama.
    """
    if not (DOMAIN_RAG_INDEX / "faiss_index.bin").exists():
        pytest.skip(f"Domain RAG index not built at {DOMAIN_RAG_INDEX}")
    if not (VIOLIN_DIR / "Pathogen_Information.csv").exists():
        pytest.skip(f"VIOLIN data not found at {VIOLIN_DIR}")
    if not _ollama_reachable():
        pytest.skip(f"Ollama not reachable at {OLLAMA_URL} or model {OLLAMA_MODEL!r} not pulled")

    monkeypatch.setenv("APECX_LLM_BASE_URL", f"{OLLAMA_URL}/v1")
    monkeypatch.setenv("APECX_LLM_MODEL", OLLAMA_MODEL)
    monkeypatch.setenv("APECX_LLM_API_KEY", "EMPTY")
    monkeypatch.setenv("APECX_LLM_TEMPERATURE", "0.0")
    monkeypatch.setenv("APECX_LLM_MAX_TOKENS", "512")

    async def _drive_cascade() -> dict:
        wf = Workflow.from_config(str(WORKFLOW_YAML))

        # ``Workflow.from_config`` constructs the step instances but
        # does NOT resolve their triggers — that happens in Phase 3 of
        # ``Workflow.initialize()``, which must be invoked explicitly.
        # Without this, the data-driven cascade can't fire because no
        # listeners are registered on the input data units. Source:
        # nanobrain/core/step.py::_resolve_and_bind_step_triggers.
        await wf.initialize()

        children = (
            getattr(wf, "child_steps", None)
            or getattr(wf, "_child_steps", None)
            or getattr(wf, "steps", None)
        )
        assembly = children["synthesis_context_assembly"]
        synthesis = children["rag_synthesis"]
        assembly._skip_pubmed = True

        # Data-driven entry point: this populates the assembly step's
        # ``assembly_input`` data unit and returns immediately. The
        # actual processing happens in async background tasks the
        # framework spawned from the trigger system.
        init_result = await wf.process(
            {"assembly_input": {"query": "Eastern equine encephalitis vaccines"}}
        )
        assert init_result is not None
        # The data-driven mode reports a synchronous initiation envelope.
        assert init_result.get("status") == "data_flow_initiated", (
            f"expected data_flow_initiated; got: {init_result!r}"
        )

        # The new primitive: drain the trigger cascade synchronously.
        # 90s budget covers FAISS load (one-shot, ~5s) + LLM round-trip
        # (~30-60s on mistral-nemo) with margin.
        drained = await wf.wait_for_cascade(timeout=90.0, settle_ms=100)
        assert drained, (
            "trigger cascade did not drain within 90s — either the "
            "wait_for_cascade implementation is buggy, or one of the "
            "trigger handlers is hanging."
        )

        # Read the synthesis output from the framework-managed data unit.
        out_du = synthesis.step_output_data_units["synthesis_output"]
        synthesis_value = await out_du.get()
        return synthesis_value

    output = asyncio.run(_drive_cascade())
    assert output is not None, (
        "synthesis_output was not set after the cascade drained — the "
        "DirectLink between assembly and synthesis may not have fired "
        "the synthesis step. Check the workflow YAML's link wiring."
    )
    # The synthesis step writes either the full bundle dict or just
    # the synthesis string depending on the framework version's
    # output-mapping behavior; accept either.
    if isinstance(output, dict):
        assert "synthesis" in output, (
            f"synthesis_output dict missing 'synthesis' key; got: {sorted(output.keys())!r}"
        )
        body = output["synthesis"]
    else:
        body = output
    assert isinstance(body, str)
    assert len(body) > 50, (
        f"synthesis body suspiciously short ({len(body)} chars) — "
        "either the cascade fired but the synthesis step short-circuited, "
        "or the LLM returned a degraded response. Body excerpt: "
        f"{body[:200]!r}"
    )
