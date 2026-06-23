"""WS1c (apecx side): SubworkflowStep.inner_workflow_name binds a REAL
reasoning-pattern workflow end-to-end (resolve -> load) against the actual
``composition/workflows`` directory.

The nanobrain-side resolver + 3-way mutual-exclusion logic is unit-tested in
nanobrain (`tests/unit/test_subworkflow_name_binding.py`). This test proves the
seam works on REAL apecx reasoning-pattern workflows on disk — no synthetic
workflows, no mocks.

Run-parity note (why there is no separate LLM-driven run test): name-binding
folds the resolved name into the SAME ``path_str`` the path branch consumes, so
``Workflow.from_config`` produces a byte-identical inner ``Workflow`` whether the
path came from a literal ``inner_workflow_path`` or a resolved
``inner_workflow_name``. The existing path-bound pattern tests already exercise
running these workflows; run-parity therefore holds by construction, and a
duplicate Ollama-driven run test would add flakiness without adding coverage.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nanobrain.core.component_base import ComponentConfigurationError
from nanobrain.library.steps.subworkflow_step import SubworkflowStep

import apecx_integration

_WF_DIR = str(Path(apecx_integration.__file__).parent / "composition" / "workflows")


def _name_bound_step(tmp_path: Path, name: str) -> SubworkflowStep:
    p = tmp_path / "name_bound_step.yml"
    p.write_text(
        f"name: nb_{name}\ninner_workflow_name: {name}\nworkflow_search_paths: ['{_WF_DIR}']\n"
    )
    return SubworkflowStep.from_config(str(p))


@pytest.mark.parametrize(
    "name,expected_yaml",
    [
        ("tdr_loop", "tdr_refine_workflow.yml"),
        ("best_of_n_loop", "best_of_n_workflow.yml"),
        ("rag_e2e_synthesis", "rag_e2e_synthesis_workflow.yml"),
    ],
)
def test_name_binds_real_reasoning_pattern(tmp_path, name, expected_yaml):
    step = _name_bound_step(tmp_path, name)
    # The inner workflow actually loaded (not None, not a stub).
    assert step.inner_workflow is not None
    # ...resolved to the canonical workflow YAML under the REAL <name>/ dir,
    # including the tdr_loop case where dir name != YAML stem.
    assert step.inner_workflow_path.name == expected_yaml
    assert step.inner_workflow_path.parent.name == name
    assert step.inner_workflow_path.is_file()


def test_unknown_pattern_fails_loud_listing_real_workflows(tmp_path):
    p = tmp_path / "s.yml"
    p.write_text(
        "name: nb_unknown\n"
        "inner_workflow_name: not_a_real_pattern\n"
        f"workflow_search_paths: ['{_WF_DIR}']\n"
    )
    with pytest.raises(ComponentConfigurationError) as exc:
        SubworkflowStep.from_config(str(p))
    msg = str(exc.value)
    assert "Available names" in msg
    # The loud error lists REAL workflows that exist on disk (not a silent miss).
    assert "tdr_loop" in msg
