"""Smoke tests for the G104 best_of_n YAML workflow.

Verifies:
  1. The workflow YAML loads via Workflow.from_config (covers cycle
     validator + step + link construction with the best_of_n_iter
     step class binding + the mode flag).
  2. The cycle through LoopController is detected and allowed.
  3. TdrIterationStep in best_of_n mode does NOT pass previous_attempt
     + critique to its writer (the key behavior difference vs tdr mode).

End-to-end real-Ollama coverage is shared with the TDR workflow's
integration test — the underlying classes + topology are identical;
the only difference is the mode flag.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest import mock

from nanobrain.core.workflow import Workflow

from apecx_integration.composition.steps.tdr_iteration_step import TdrIterationStep

_BEST_OF_N_WORKFLOW = (
    Path(__file__).resolve().parents[1].parent
    / "src"
    / "apecx_integration"
    / "composition"
    / "workflows"
    / "best_of_n_loop"
    / "best_of_n_workflow.yml"
)


def test_workflow_yaml_path_exists():
    assert _BEST_OF_N_WORKFLOW.is_file(), f"Missing workflow YAML at {_BEST_OF_N_WORKFLOW}"


def test_workflow_loads_and_cycle_validator_passes():
    """Same G18-Step-2 validator path as the TDR workflow. Without
    the framework fixes from G99, this would fail at load."""
    wf = Workflow.from_config(str(_BEST_OF_N_WORKFLOW))

    assert "best_of_n_iter" in wf.child_steps
    assert "loop_gate" in wf.child_steps

    expected_links = {
        "input_to_iter",
        "iter_to_final_pass",
        "iter_to_loop_gate",
        "loop_continue_to_iter",
        "loop_exhausted_to_final",
    }
    assert set(wf.step_links.keys()) == expected_links


def test_iteration_step_is_in_best_of_n_mode():
    """The iteration step's config sets mode: best_of_n, distinct
    from the TDR workflow's mode: tdr default."""
    wf = Workflow.from_config(str(_BEST_OF_N_WORKFLOW))
    step = wf.child_steps["best_of_n_iter"]
    assert isinstance(step, TdrIterationStep)
    assert step._mode == "best_of_n"


class TestBestOfNModeBehavior:
    """The key behavior difference between modes is whether previous_attempt
    + critique are passed to the writer. We test this with a fake writer
    that captures its input."""

    def _build_step(self, mode: str) -> TdrIterationStep:
        from pathlib import Path

        wrapper_path = (
            Path(__file__).resolve().parents[1].parent
            / "src"
            / "apecx_integration"
            / "composition"
            / "workflows"
            / "best_of_n_loop"
            / "steps"
            / "best_of_n_iteration.yml"
        )
        if mode == "tdr":
            wrapper_path = (
                Path(__file__).resolve().parents[1].parent
                / "src"
                / "apecx_integration"
                / "composition"
                / "workflows"
                / "tdr_loop"
                / "steps"
                / "tdr_iteration.yml"
            )
        return TdrIterationStep.from_config(str(wrapper_path))

    def test_best_of_n_mode_does_not_pass_previous_attempt_to_writer(self):
        """In best_of_n mode, each iteration is INDEPENDENT — the
        writer should NOT see previous_attempt or critique. Test by
        invoking the step at prior_iteration > 0 and inspecting the
        writer input."""
        step = self._build_step("best_of_n")

        captured_writer_inputs: list[dict] = []

        async def fake_writer_process(input_data, **kwargs):
            captured_writer_inputs.append(dict(input_data))
            return {"code_source": "def fake(): pass", "function_name_verified": "fake"}

        async def fake_exec_process(input_data, **kwargs):
            return {
                "stdout": "",
                "stderr": "",
                "returncode": 0,
                "exec_succeeded": False,
                "elapsed_seconds": 0.01,
            }

        # Simulate a back-edge envelope (prior_iteration > 0).
        envelope_wrapped = {
            "allow_continue": True,
            "loop_exhausted": False,
            "iteration": 1,
            "max_iterations": 3,
            "payload": {
                "code_spec": "Write a function",
                "function_name": "f",
                "test_code": "assert f() == 1",
                "code_source": "def f(): return 2",  # prior attempt
                "critique": "stderr from prior",  # prior critique
            },
        }

        with (
            mock.patch.object(step._writer, "process", side_effect=fake_writer_process),
            mock.patch.object(step._executor, "process", side_effect=fake_exec_process),
        ):
            asyncio.run(step.process(envelope_wrapped))

        # ONE writer call (this iteration).
        assert len(captured_writer_inputs) == 1
        wi = captured_writer_inputs[0]
        # The code_spec passes through (basic context).
        assert wi["code_spec"] == "Write a function"
        # The key best_of_n contract: previous_attempt + critique are
        # NOT passed to the writer. Each sample is independent.
        assert "previous_attempt" not in wi, (
            "best_of_n mode leaked previous_attempt to writer — each sample should be independent."
        )
        assert "critique" not in wi, (
            "best_of_n mode leaked critique to writer — each sample should be independent."
        )

    def test_tdr_mode_does_pass_previous_attempt_to_writer(self):
        """Regression guard for tdr mode (default). The MODE flag's
        effect should only fire when set to best_of_n."""
        step = self._build_step("tdr")

        captured_writer_inputs: list[dict] = []

        async def fake_writer_process(input_data, **kwargs):
            captured_writer_inputs.append(dict(input_data))
            return {"code_source": "def fake(): pass", "function_name_verified": "fake"}

        async def fake_exec_process(input_data, **kwargs):
            return {
                "stdout": "",
                "stderr": "",
                "returncode": 0,
                "exec_succeeded": False,
                "elapsed_seconds": 0.01,
            }

        envelope_wrapped = {
            "allow_continue": True,
            "loop_exhausted": False,
            "iteration": 1,
            "max_iterations": 3,
            "payload": {
                "code_spec": "Write a function",
                "function_name": "f",
                "test_code": "assert f() == 1",
                "code_source": "def f(): return 2",
                "critique": "stderr from prior",
            },
        }

        with (
            mock.patch.object(step._writer, "process", side_effect=fake_writer_process),
            mock.patch.object(step._executor, "process", side_effect=fake_exec_process),
        ):
            asyncio.run(step.process(envelope_wrapped))

        wi = captured_writer_inputs[0]
        # tdr mode DOES pass these (the canonical TDR contract).
        assert wi.get("previous_attempt") == "def f(): return 2"
        assert wi.get("critique") == "stderr from prior"
