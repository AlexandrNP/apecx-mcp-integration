"""Unit tests for the recursive workflow inspector (EO-02)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apecx_integration.composition.inspection.workflow_inspector import inspect_workflow


def _tdr_workflow_path() -> Path:
    return (
        Path(__file__).resolve().parents[1].parent
        / "src"
        / "apecx_integration"
        / "composition"
        / "workflows"
        / "tdr_loop"
        / "tdr_refine_workflow.yml"
    )


def test_inspect_real_tdr_workflow():
    insp = inspect_workflow(_tdr_workflow_path())
    assert insp.name == "tdr_refine_workflow"
    assert insp.config_version == 2
    assert insp.input_data_units == ["workflow_input"]
    assert insp.output_data_units == ["final_code"]

    step_names = {s.name for s in insp.steps}
    assert step_names == {"tdr_iter", "loop_gate"}

    tdr_iter = next(s for s in insp.steps if s.name == "tdr_iter")
    assert "TdrIterationStep" in tdr_iter.class_path
    assert tdr_iter.config_path == "steps/tdr_iteration.yml"
    # The step config file was resolved (its data units are lists, possibly populated).
    assert isinstance(tdr_iter.input_data_units, list)
    assert isinstance(tdr_iter.output_data_units, list)

    # Links parsed, including the ConditionalLink predicate (yaml true -> Python True).
    link_names = {link.name for link in insp.links}
    assert "iter_to_final_pass" in link_names
    cond = next(link for link in insp.links if link.name == "iter_to_final_pass")
    assert "ConditionalLink" in cond.class_path
    assert cond.condition == {"op": "eq", "field": "exec_succeeded", "value": True}


def test_recursion_into_nested_workflow(tmp_path):
    inner = tmp_path / "inner.yml"
    inner.write_text(
        "name: inner_wf\n"
        "config_version: 2\n"
        "steps:\n"
        "  leaf:\n"
        "    class: pkg.Leaf\n"
        "    config:\n"
        "      input_data_units: {a: {}}\n"
        "      output_data_units: {b: {}}\n"
    )
    outer = tmp_path / "outer.yml"
    outer.write_text(
        "name: outer_wf\n"
        "config_version: 2\n"
        "steps:\n"
        "  sub:\n"
        "    class: nanobrain.core.workflow.Workflow\n"
        "    config: inner.yml\n"
    )
    insp = inspect_workflow(outer, max_depth=3)
    sub = insp.steps[0]
    assert sub.name == "sub"
    assert sub.nested_workflow is not None
    assert sub.nested_workflow.name == "inner_wf"
    leaf = sub.nested_workflow.steps[0]
    assert leaf.name == "leaf"
    assert leaf.input_data_units == ["a"]
    assert leaf.output_data_units == ["b"]
    assert insp.truncated is False


def test_depth_cap_truncates(tmp_path):
    inner = tmp_path / "inner.yml"
    inner.write_text("name: inner_wf\nsteps:\n  leaf:\n    class: pkg.Leaf\n")
    outer = tmp_path / "outer.yml"
    outer.write_text("name: outer_wf\nsteps:\n  sub:\n    class: pkg.W\n    config: inner.yml\n")
    insp = inspect_workflow(outer, max_depth=0)
    assert insp.steps[0].nested_workflow is None
    assert insp.truncated is True


def test_missing_step_config_raises_loudly(tmp_path):
    wf = tmp_path / "wf.yml"
    wf.write_text("name: x\nsteps:\n  s:\n    class: c\n    config: does_not_exist.yml\n")
    with pytest.raises(FileNotFoundError):
        inspect_workflow(wf)


def test_non_mapping_yaml_raises(tmp_path):
    bad = tmp_path / "bad.yml"
    bad.write_text("- a\n- b\n")
    with pytest.raises(ValueError):
        inspect_workflow(bad)


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        inspect_workflow(tmp_path / "nope.yml")
