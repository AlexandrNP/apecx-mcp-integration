"""CW-AN1 — unit tests for WorkflowAnalysisStep.

Pure-Python step; no LLM. Tests cover:
  1. Loads via from_config.
  2. workflow_path input is loaded + parsed.
  3. workflow_dict input bypasses file IO.
  4. Missing / unparseable input raises with clear message.
  5. Empty workflow → ``no_steps`` issue.
  6. v1 DirectLink without auto_transfer → flagged.
  7. v2 DirectLink without auto_transfer → NOT flagged (G7 auto-injects).
  8. Link endpoint referencing nonexistent step → flagged.
  9. Topology summary correctly reports linear vs fan-out.
 10. Real apecx workflow (code_reflection_workflow.yml) analyzes cleanly.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest

from apecx_integration.composition.steps.workflow_analysis_step import (
    WorkflowAnalysisStep,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _stage_step(tmp_path: Path) -> WorkflowAnalysisStep:
    p = tmp_path / "wf_analyzer.yml"
    p.write_text("name: analyzer_test\n")
    return WorkflowAnalysisStep.from_config(str(p))


def test_loads_via_from_config(tmp_path):
    step = _stage_step(tmp_path)
    assert step.name == "analyzer_test"


def test_workflow_dict_input_skips_file_io(tmp_path):
    step = _stage_step(tmp_path)
    wf_dict = {
        "name": "minimal",
        "description": "Tiny workflow.",
        "config_version": 2,
        "steps": {"only_step": {"class": "x.Y"}},
        "links": {},
    }
    result = asyncio.run(step.process({"workflow_dict": wf_dict}))
    assert result["workflow_name"] == "minimal"
    assert result["config_version"] == 2
    assert len(result["steps"]) == 1
    assert result["steps"][0]["step_name"] == "only_step"


def test_workflow_path_input_loads_yaml(tmp_path):
    yml = tmp_path / "wf.yml"
    yml.write_text(
        textwrap.dedent(
            """\
            name: from_path
            description: Test workflow.
            config_version: 2
            steps:
              a:
                class: pkg.A
              b:
                class: pkg.B
            links:
              a_to_b:
                class: nanobrain.core.link.DirectLink
                config:
                  link_type: direct
                  source: a.out
                  target: b.in
                  auto_transfer: true
            """
        )
    )
    step = _stage_step(tmp_path)
    result = asyncio.run(step.process({"workflow_path": str(yml)}))
    assert result["workflow_name"] == "from_path"
    assert len(result["steps"]) == 2
    assert len(result["links"]) == 1
    assert result["links"][0]["auto_transfer"] is True


def test_missing_input_raises_clear_error(tmp_path):
    step = _stage_step(tmp_path)
    with pytest.raises(ValueError, match="workflow_path"):
        asyncio.run(step.process({}))


def test_nonexistent_path_raises(tmp_path):
    step = _stage_step(tmp_path)
    with pytest.raises(ValueError, match="does not exist"):
        asyncio.run(step.process({"workflow_path": "/nonexistent/path.yml"}))


def test_malformed_yaml_raises(tmp_path):
    yml = tmp_path / "bad.yml"
    yml.write_text(":\n--garbage--\n  - [unbalanced")
    step = _stage_step(tmp_path)
    with pytest.raises(ValueError, match="failed parse"):
        asyncio.run(step.process({"workflow_path": str(yml)}))


def test_empty_workflow_flags_no_steps_issue(tmp_path):
    step = _stage_step(tmp_path)
    result = asyncio.run(
        step.process({"workflow_dict": {"name": "empty", "steps": {}, "links": {}}})
    )
    codes = [i["code"] for i in result["issues"]]
    assert "no_steps" in codes


def test_v1_directlink_without_auto_transfer_is_flagged(tmp_path):
    step = _stage_step(tmp_path)
    wf = {
        "name": "v1_workflow",
        "config_version": 1,
        "steps": {"a": {"class": "x.A"}},
        "links": {
            "bad_link": {
                "class": "nanobrain.core.link.DirectLink",
                "config": {"source": "a.out", "target": "a.in"},
            }
        },
    }
    result = asyncio.run(step.process({"workflow_dict": wf}))
    codes = [i["code"] for i in result["issues"]]
    assert "directlink_missing_auto_transfer" in codes


def test_v2_directlink_without_auto_transfer_is_NOT_flagged(tmp_path):
    """Under v2 the framework auto-injects auto_transfer:true at load.
    The analyzer trusts the v2 contract and doesn't flag."""
    step = _stage_step(tmp_path)
    wf = {
        "name": "v2_workflow",
        "config_version": 2,
        "steps": {"a": {"class": "x.A"}},
        "links": {
            "implicit_link": {
                "class": "nanobrain.core.link.DirectLink",
                "config": {"source": "a.out", "target": "a.in"},
            }
        },
    }
    result = asyncio.run(step.process({"workflow_dict": wf}))
    codes = [i["code"] for i in result["issues"]]
    assert "directlink_missing_auto_transfer" not in codes


def test_link_endpoint_unresolved_flagged(tmp_path):
    step = _stage_step(tmp_path)
    wf = {
        "name": "broken",
        "config_version": 2,
        "steps": {"a": {"class": "x.A"}},
        "links": {
            "ghost_link": {
                "class": "nanobrain.core.link.DirectLink",
                "config": {
                    "source": "nonexistent_step.out",
                    "target": "a.in",
                    "auto_transfer": True,
                },
            }
        },
    }
    result = asyncio.run(step.process({"workflow_dict": wf}))
    codes = [i["code"] for i in result["issues"]]
    assert "link_endpoint_unresolved" in codes


def test_topology_summary_pins_linear(tmp_path):
    step = _stage_step(tmp_path)
    wf = {
        "name": "linear",
        "config_version": 2,
        "steps": {"a": {"class": "x.A"}, "b": {"class": "x.B"}},
        "links": {
            "a_to_b": {
                "class": "nanobrain.core.link.DirectLink",
                "config": {
                    "source": "a.out",
                    "target": "b.in",
                    "auto_transfer": True,
                },
            }
        },
    }
    result = asyncio.run(step.process({"workflow_dict": wf}))
    assert "linear pipeline" in result["topology_summary"]


def test_real_apecx_workflow_analyzes_cleanly(tmp_path):
    """Smoke-test against the shipped code_reflection_workflow.yml.
    No issues expected for a known-good workflow."""
    step = _stage_step(tmp_path)
    yml = (
        REPO_ROOT
        / "src"
        / "apecx_integration"
        / "composition"
        / "workflows"
        / "code_writing"
        / "code_reflection_workflow.yml"
    )
    result = asyncio.run(step.process({"workflow_path": str(yml)}))
    assert result["workflow_name"] == "code_reflection_workflow"
    assert len(result["steps"]) == 2
    # All 5 DirectLinks have auto_transfer:true.
    for link in result["links"]:
        assert link["auto_transfer"] is True
    # No issues on a known-good workflow.
    codes = [i["code"] for i in result["issues"]]
    assert codes == [], f"unexpected issues: {result['issues']}"
