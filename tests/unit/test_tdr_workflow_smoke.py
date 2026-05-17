"""Smoke tests for the G99 TDR-as-YAML workflow.

These tests do NOT call any LLM and do NOT execute any code. They
only validate that:

1. The YAML loads via Workflow.from_config (covers schema validation,
   path resolution, step + link construction, executor wiring).
2. The cycle through LoopController is detected and allowed by the
   workflow graph validator (G18 Step 2 extension).
3. The TdrIterationStep can be constructed standalone (covers the
   sub-step instantiation chain: writer + executor).
4. The envelope-unwrap logic correctly distinguishes initial input
   from loop-gate-wrapped back-edge input.

End-to-end execution is covered by the gated integration test at
tests/integration/test_tdr_workflow_against_ollama.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nanobrain.core.workflow import Workflow

from apecx_integration.composition.steps.tdr_iteration_step import TdrIterationStep

_WORKFLOW_YAML = (
    Path(__file__).resolve().parents[1].parent
    / "src"
    / "apecx_integration"
    / "composition"
    / "workflows"
    / "tdr_loop"
    / "tdr_refine_workflow.yml"
)


def test_workflow_yaml_path_exists():
    """Guard against accidental moves — keeps the path in the test in
    sync with the actual repo layout."""
    assert _WORKFLOW_YAML.is_file(), f"Missing workflow YAML at {_WORKFLOW_YAML}"


def test_workflow_loads_and_cycle_validator_passes():
    """The workflow contains a back-edge through LoopController.
    Without G18 Step 2's _all_cycles_pass_through_loop_controller
    validator extension, Workflow.from_config would raise here.
    """
    wf = Workflow.from_config(str(_WORKFLOW_YAML))

    # Sanity: the two declared steps both materialized.
    assert "tdr_iter" in wf.child_steps
    assert "loop_gate" in wf.child_steps

    # Sanity: all five links materialized.
    expected_links = {
        "input_to_iter",
        "iter_to_final_pass",
        "iter_to_loop_gate",
        "loop_continue_to_iter",
        "loop_exhausted_to_final",
    }
    assert set(wf.step_links.keys()) == expected_links, (
        f"Link set mismatch — got {set(wf.step_links.keys())}, expected {expected_links}"
    )

    # Sanity: loop_gate is recognized as a LoopController.
    from nanobrain.library.steps.loop_controller import LoopController

    assert isinstance(wf.child_steps["loop_gate"], LoopController)
    assert wf.child_steps["loop_gate"].COMPONENT_TYPE == "loop_controller"


def test_tdr_iteration_step_constructs_standalone():
    """TdrIterationStep needs CodeWriteStep + IsolatedPyExecStep to
    instantiate via from_config. This test exercises the
    cross-step instantiation chain without the surrounding workflow.
    """
    step_yaml = _WORKFLOW_YAML.parent / "steps" / "tdr_iteration.yml"
    step = TdrIterationStep.from_config(str(step_yaml))
    assert step.name == "tdr_iter"
    # Inner sub-steps must be live, not lazy stubs.
    assert step._writer is not None
    assert step._executor is not None
    # Inner instances have their expected COMPONENT_TYPEs.
    assert step._writer.COMPONENT_TYPE == "code_write_step"
    assert step._executor.COMPONENT_TYPE == "isolated_py_exec_step"


class TestEnvelopeUnwrap:
    """Unit tests for the private envelope-shape detector. Cycle
    correctness depends on this distinguishing the initial-input
    shape from the LoopController-wrapped back-edge shape."""

    def test_initial_envelope_passes_through(self):
        initial = {
            "code_spec": "Write a function",
            "function_name": "f",
            "test_code": "assert f() == 1",
        }
        envelope, iteration = TdrIterationStep._unwrap_envelope(initial)
        assert envelope is initial
        assert iteration == 0

    def test_loop_gate_wrapped_envelope_unwrapped(self):
        inner = {
            "code_spec": "spec",
            "function_name": "f",
            "test_code": "assert True",
            "code_source": "def f(): return 1",
            "exec_succeeded": False,
            "critique": "stderr...",
        }
        wrapped = {
            "allow_continue": True,
            "loop_exhausted": False,
            "iteration": 2,
            "max_iterations": 3,
            "payload": inner,
        }
        envelope, iteration = TdrIterationStep._unwrap_envelope(wrapped)
        assert envelope is inner
        assert iteration == 2

    def test_ambiguous_shape_raises(self):
        """Half-wrapped (one control key but not the other) is a
        workflow-author error — surface loudly rather than silently
        guess."""
        with pytest.raises(ValueError, match="ambiguous input shape"):
            TdrIterationStep._unwrap_envelope({"allow_continue": True})
        with pytest.raises(ValueError, match="ambiguous input shape"):
            TdrIterationStep._unwrap_envelope({"payload": {"a": 1}})

    def test_non_dict_payload_raises(self):
        with pytest.raises(ValueError, match="must be a dict"):
            TdrIterationStep._unwrap_envelope({"allow_continue": True, "payload": "not-a-dict"})


class TestCritiqueFormat:
    """The critique string is the only data path from a failed exec
    to the next iteration's revision call. If its shape changes
    silently the LLM gets an unparseable critique and the loop
    iterates without improvement."""

    def test_passing_exec_yields_empty_critique(self):
        result = TdrIterationStep._format_critique(
            {
                "exec_succeeded": True,
                "stderr": "",
                "returncode": 0,
            }
        )
        assert result == ""

    def test_failing_exec_includes_stderr(self):
        result = TdrIterationStep._format_critique(
            {
                "exec_succeeded": False,
                "stderr": "AssertionError: 1 != 2",
                "returncode": 1,
            }
        )
        assert "AssertionError" in result
        assert "previous attempt failed" in result

    def test_failing_exec_with_empty_stderr_still_useful(self):
        """A subprocess that fails with no stderr would otherwise
        produce an empty critique. We synthesize one from returncode."""
        result = TdrIterationStep._format_critique(
            {
                "exec_succeeded": False,
                "stderr": "",
                "returncode": 124,
            }
        )
        assert "124" in result
        assert "no stderr" in result

    def test_long_stderr_is_truncated(self):
        long_stderr = "x" * 5000
        result = TdrIterationStep._format_critique(
            {
                "exec_succeeded": False,
                "stderr": long_stderr,
                "returncode": 1,
            }
        )
        # Should be truncated to ≤ 2000 chars of stderr (last 2000)
        # plus the prefix text. Verify the prefix is present and the
        # truncated portion is the TAIL (most-recent stderr is most
        # relevant).
        assert "previous attempt failed" in result
        # Last 2000 chars of stderr — should contain "x"s at the end
        assert result.endswith("x" * 100)  # spot-check tail preserved
