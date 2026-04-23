"""T02 Phase 4 finish: the top-level violin_bvbrc_workflow.yml loads
via ``Workflow.from_config(...)``.

This proves the 8 step-YAMLs compose cleanly (6 original + Steps 2
and 6 wired to BVBRCSnapshotTool). It does NOT prove the workflow
runs end-to-end — three steps are still missing wrapper YAMLs (see
the workflow YAML header for per-step blockers).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nanobrain.core.step import BaseStep
from nanobrain.core.workflow import Workflow

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = (
    REPO_ROOT
    / "src"
    / "apecx_integration"
    / "composition"
    / "workflows"
    / "violin_bvbrc"
)
WORKFLOW_YAML = WORKFLOW_DIR / "violin_bvbrc_workflow.yml"
STEP2_YAML = WORKFLOW_DIR / "steps" / "bvbrc_snapshot_match.yml"
STEP6_YAML = WORKFLOW_DIR / "steps" / "genomic_annotation.yml"
STEP1_YAML = WORKFLOW_DIR / "steps" / "entity_extraction.yml"
STEP3C_YAML = WORKFLOW_DIR / "steps" / "synonym_llm_proposals.yml"
STEP5_YAML = WORKFLOW_DIR / "steps" / "violin_entity_lookup.yml"


@pytest.fixture
def chdir_repo_root(monkeypatch):
    """Both the Step 2 and Step 6 YAMLs reference the tool YAML via a
    repo-root-relative path (``src/apecx_integration/...``). That
    resolution only works when cwd == repo root. Pytest's default
    cwd is the repo root already, but make it explicit so this test
    passes regardless of how the test binary is invoked.
    """
    monkeypatch.chdir(REPO_ROOT)


def test_skeleton_workflow_yaml_loads(chdir_repo_root) -> None:
    """Workflow.from_config on the skeleton YAML completes without
    raising. The step-composition internal shape (where nanobrain
    puts the instantiated steps) is not asserted here because the
    framework's `Workflow` attribute layout isn't documented cleanly
    — an internal-attribute assertion would be fragile. A runtime
    execution test belongs with T01 vertical slice.
    """
    assert WORKFLOW_YAML.is_file(), WORKFLOW_YAML
    workflow = Workflow.from_config(str(WORKFLOW_YAML))
    assert workflow.name == "violin_bvbrc_workflow"
    assert workflow is not None


def test_step2_bvbrc_snapshot_match_loads(chdir_repo_root) -> None:
    """Step 2 (EnhancedBVBRCDataAcquisitionStep) takes its tool + two
    LLM-agent configs under ``tools:``. Loading this YAML in
    isolation guards against regressions in the tools-block schema
    specifically (Step 6 uses a different shape; see below)."""
    assert STEP2_YAML.is_file(), STEP2_YAML
    step = BaseStep.from_config(str(STEP2_YAML))
    assert step.name == "bvbrc_snapshot_match"


def test_step6_genomic_annotation_loads(chdir_repo_root) -> None:
    """Step 6 (BVBRCDataAcquisitionStep) uses *top-level* config keys
    — ``synonym_detection_agent`` + ``bvbrc_config_file`` — not the
    ``tools:`` block Step 2 uses. This test pins that shape so we
    don't silently regress back to the tools: layout."""
    assert STEP6_YAML.is_file(), STEP6_YAML
    step = BaseStep.from_config(str(STEP6_YAML))
    assert step.name == "genomic_annotation"


def test_step1_entity_extraction_loads(chdir_repo_root) -> None:
    """Step 1 (EntityExtractionStep) is the thinnest of the three
    db-integration wrappers — no extra config beyond StepConfig. Loads
    via from_config without any operator-side LLM env vars; the wrapped
    function only resolves env vars at process() time, not init time."""
    assert STEP1_YAML.is_file(), STEP1_YAML
    step = BaseStep.from_config(str(STEP1_YAML))
    assert step.name == "entity_extraction"


def test_step3c_synonym_llm_proposals_loads(chdir_repo_root) -> None:
    """Step 3c (SynonymLLMProposalsStep) carries an optional ``data_dir``
    field. The wrapper YAML sets it to ``null`` (falls through to
    APECX_DB_DATA_DIR env var). Loadability proves the optional-config
    contract round-trips cleanly."""
    assert STEP3C_YAML.is_file(), STEP3C_YAML
    step = BaseStep.from_config(str(STEP3C_YAML))
    assert step.name == "synonym_llm_proposals"


def test_step5_violin_entity_lookup_loads(chdir_repo_root) -> None:
    """Step 5 (ViolinEntityLookupStep) is the only no-LLM wrapper; pure
    pandas join over VIOLIN tables. Loadability is identical-shape to
    Step 3c (data_dir override field) — pinned separately so a future
    schema drift on either step shows up in the test name."""
    assert STEP5_YAML.is_file(), STEP5_YAML
    step = BaseStep.from_config(str(STEP5_YAML))
    assert step.name == "violin_entity_lookup"
