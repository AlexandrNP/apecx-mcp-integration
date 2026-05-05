"""Framework-level loadability tests for the rag_e2e_synthesis workflow.

The companion test_rag_e2e_pipeline.py exercises the steps' process()
methods individually with real Ollama / real domain-RAG / real CSVs.
This file pins the OTHER half of the contract: that the workflow YAML
+ the two step YAMLs compose cleanly through nanobrain's
``Workflow.from_config`` and ``BaseStep.from_config`` loaders.

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

from pathlib import Path

import pytest
from nanobrain.core.step import BaseStep
from nanobrain.core.workflow import Workflow

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = (
    REPO_ROOT / "src" / "apecx_integration" / "composition" / "workflows" / "rag_e2e_synthesis"
)
WORKFLOW_YAML = WORKFLOW_DIR / "rag_e2e_synthesis_workflow.yml"
STEP_ASSEMBLY_YAML = WORKFLOW_DIR / "steps" / "synthesis_context_assembly.yml"
STEP_SYNTHESIS_YAML = WORKFLOW_DIR / "steps" / "rag_synthesis.yml"


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
