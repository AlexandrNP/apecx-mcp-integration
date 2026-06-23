"""ReasoningPatternStep: bind a REAL apecx reasoning-pattern workflow by name
with a PORTABLE config (only ``inner_workflow_name`` — no absolute search path).

This exercises the apecx subclass's ``_default_workflow_search_paths`` override
end-to-end (resolve apecx's composition/workflows dir from the package location,
then load the real workflow), which is the genuine consumption of nanobrain's
``_default_workflow_search_paths`` classmethod. Real workflows on disk, no mocks.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nanobrain.core.component_base import ComponentConfigurationError

import apecx_integration
from apecx_integration.composition.steps.reasoning_pattern_step import (
    ReasoningPatternStep,
)

_WF_DIR = Path(apecx_integration.__file__).parent / "composition" / "workflows"


@pytest.mark.parametrize(
    "name,expected_yaml",
    [
        ("tdr_loop", "tdr_refine_workflow.yml"),
        ("best_of_n_loop", "best_of_n_workflow.yml"),
        ("rag_e2e_synthesis", "rag_e2e_synthesis_workflow.yml"),
    ],
)
def test_binds_real_pattern_with_portable_config(tmp_path, name, expected_yaml):
    # Config carries ONLY the name — no workflow_search_paths. The subclass
    # supplies apecx's workflows dir, so this config is portable across installs.
    p = tmp_path / "pattern_step.yml"
    p.write_text(f"name: nb_{name}\ninner_workflow_name: {name}\n")
    step = ReasoningPatternStep.from_config(str(p))
    assert step.inner_workflow is not None
    assert step.inner_workflow_path.name == expected_yaml
    assert step.inner_workflow_path.parent.name == name
    assert step.inner_workflow_path.is_file()


def test_unknown_pattern_fails_loud(tmp_path):
    p = tmp_path / "pattern_step.yml"
    p.write_text("name: nb_x\ninner_workflow_name: not_a_real_pattern\n")
    with pytest.raises(ComponentConfigurationError) as exc:
        ReasoningPatternStep.from_config(str(p))
    msg = str(exc.value)
    assert "Available names" in msg
    assert "tdr_loop" in msg  # lists real workflows that exist on disk


def test_explicit_search_paths_still_respected(tmp_path):
    # An explicit workflow_search_paths in config must take precedence over the
    # subclass default (so callers can point at a different dir when needed).
    # Pointing at an empty tmp dir => the real pattern is NOT found there =>
    # FAIL-LOUD, proving the explicit override replaced the default.
    p = tmp_path / "pattern_step.yml"
    p.write_text(
        f"name: nb_o\ninner_workflow_name: tdr_loop\nworkflow_search_paths: ['{tmp_path}']\n"
    )
    with pytest.raises(ComponentConfigurationError, match="Available names"):
        ReasoningPatternStep.from_config(str(p))


@pytest.mark.parametrize(
    "wrapper_rel,inner_yaml",
    [
        ("tdr_loop/tdr_loop_pattern.yml", "tdr_refine_workflow.yml"),
        ("best_of_n_loop/best_of_n_pattern.yml", "best_of_n_workflow.yml"),
        ("rag_e2e_synthesis/rag_e2e_synthesis_pattern.yml", "rag_e2e_synthesis_workflow.yml"),
    ],
)
def test_committed_pattern_wrapper_loads(wrapper_rel, inner_yaml):
    # The committed reusable-pattern wrappers must actually instantiate (a wrapper
    # that doesn't load would be a broken reference advertised as reusable — a
    # silent gap). Loads from the REAL committed file, no tmp/mocks.
    wrapper = _WF_DIR / wrapper_rel
    assert wrapper.is_file(), f"committed pattern wrapper missing: {wrapper}"
    step = ReasoningPatternStep.from_config(str(wrapper))
    assert step.inner_workflow is not None
    assert step.inner_workflow_path.name == inner_yaml
    # A generous outer timeout was set (not the silent 60s default that would
    # prematurely TimeoutError a multi-minute reasoning pattern).
    assert step._timeout_seconds >= 300
