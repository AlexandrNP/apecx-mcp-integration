"""Unit tests for G106 ``tdr_with_rollback`` mode.

Pins the behavior of the mode in ISOLATION (direct .process() calls
on a constructed TdrIterationStep, mock writer + executor). The mode
is currently dead code under the production TDR workflow topology
(short-circuit on first pass), but the cache mechanism is real and
will be load-bearing for future non-short-circuiting patterns.

These tests document what the mode actually does — which is NOT
'fixes TDR's uniquely breaks failures'. See the step's docstring
for the honest framing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest import mock

import pytest

from apecx_integration.composition.steps.tdr_iteration_step import TdrIterationStep


def _build_rollback_step() -> TdrIterationStep:
    """Build a TdrIterationStep instance in tdr_with_rollback mode
    using the TDR workflow's wrapper YAMLs (writer + executor) as
    the inner steps. We then patch the writer/executor process()
    methods to control behavior in tests."""
    # Reuse the TDR workflow's step wrapper paths. We construct a
    # config dict that the FromConfigBase loader can consume.
    repo_root = Path(__file__).resolve().parents[1].parent
    tdr_steps_dir = (
        repo_root / "src" / "apecx_integration" / "composition" / "workflows" / "tdr_loop" / "steps"
    )

    import tempfile

    import yaml

    cfg = {
        "class": "apecx_integration.composition.steps.tdr_iteration_step.TdrIterationStep",
        "name": "tdr_iter_with_rollback",
        "description": "Test instance",
        "mode": "tdr_with_rollback",
        "writer_config_path": str(tdr_steps_dir / "code_writer.yml"),
        "executor_config_path": str(tdr_steps_dir / "executor.yml"),
        "input_data_units": {
            "in": {"class": "nanobrain.core.data_unit.DataUnitMemory", "name": "in"}
        },
        "output_data_units": {
            "out": {"class": "nanobrain.core.data_unit.DataUnitMemory", "name": "out"}
        },
        "triggers": [
            {
                "class": "nanobrain.core.trigger.DataUnitChangeTrigger",
                "data_unit": "in",
            }
        ],
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
        yaml.safe_dump(cfg, f)
        path = f.name
    return TdrIterationStep.from_config(path)


class TestRollbackCacheBehavior:
    """The mode tracks _best_envelope across .process() calls on the
    same instance. When current iteration fails and a prior succeeded,
    emit the cached prior success."""

    def test_mode_is_recognized(self):
        step = _build_rollback_step()
        assert step._mode == "tdr_with_rollback"
        assert step._best_envelope is None

    def test_first_call_passing_caches_envelope(self):
        step = _build_rollback_step()

        async def fake_writer(input_data, **kwargs):
            return {"code_source": "def f(): return 1", "function_name_verified": "f"}

        async def fake_exec_pass(input_data, **kwargs):
            return {
                "stdout": "",
                "stderr": "",
                "returncode": 0,
                "exec_succeeded": True,
                "elapsed_seconds": 0.01,
            }

        envelope = {
            "code_spec": "x",
            "function_name": "f",
            "test_code": "assert f() == 1",
        }

        with (
            mock.patch.object(step._writer, "process", side_effect=fake_writer),
            mock.patch.object(step._executor, "process", side_effect=fake_exec_pass),
        ):
            result = asyncio.run(step.process(envelope))

        assert result["exec_succeeded"] is True
        assert result["code_source"] == "def f(): return 1"
        # Cache populated.
        assert step._best_envelope is not None
        assert step._best_envelope["code_source"] == "def f(): return 1"

    def test_failure_after_cached_success_rolls_back(self):
        """The headline behavior: if a prior iteration succeeded and
        the current iteration fails, emit the cached success rather
        than the failed revision."""
        step = _build_rollback_step()

        writer_responses = iter(
            [
                {"code_source": "def f(): return 1", "function_name_verified": "f"},
                {"code_source": "def f(): return WRONG", "function_name_verified": "f"},
            ]
        )
        exec_responses = iter(
            [
                {
                    "stdout": "",
                    "stderr": "",
                    "returncode": 0,
                    "exec_succeeded": True,
                    "elapsed_seconds": 0.01,
                },
                {
                    "stdout": "",
                    "stderr": "NameError: WRONG",
                    "returncode": 1,
                    "exec_succeeded": False,
                    "elapsed_seconds": 0.01,
                },
            ]
        )

        async def fake_writer(input_data, **kwargs):
            return next(writer_responses)

        async def fake_exec(input_data, **kwargs):
            return next(exec_responses)

        envelope = {
            "code_spec": "x",
            "function_name": "f",
            "test_code": "assert f() == 1",
        }

        with (
            mock.patch.object(step._writer, "process", side_effect=fake_writer),
            mock.patch.object(step._executor, "process", side_effect=fake_exec),
        ):
            # First call: succeeds, cache populated.
            asyncio.run(step.process(envelope))

            # Second call: simulate back-edge envelope with prior_iteration=1.
            second_input = {
                "allow_continue": True,
                "loop_exhausted": False,
                "iteration": 1,
                "max_iterations": 3,
                "payload": envelope,
            }
            result = asyncio.run(step.process(second_input))

        # Rollback: emitted envelope is the CACHED success, not the
        # failed revision.
        assert result["exec_succeeded"] is True
        assert result["code_source"] == "def f(): return 1"
        # iteration counter reflects the actual current iteration
        # (observability — operators see we DID try a second time).
        assert result["iteration"] == 2

    def test_failure_with_no_cached_success_emits_current(self):
        """No prior success → emit current (regular TDR escalation)."""
        step = _build_rollback_step()

        async def fake_writer(input_data, **kwargs):
            return {"code_source": "def f(): return WRONG", "function_name_verified": "f"}

        async def fake_exec(input_data, **kwargs):
            return {
                "stdout": "",
                "stderr": "NameError",
                "returncode": 1,
                "exec_succeeded": False,
                "elapsed_seconds": 0.01,
            }

        envelope = {
            "code_spec": "x",
            "function_name": "f",
            "test_code": "assert f() == 1",
        }

        with (
            mock.patch.object(step._writer, "process", side_effect=fake_writer),
            mock.patch.object(step._executor, "process", side_effect=fake_exec),
        ):
            result = asyncio.run(step.process(envelope))

        assert result["exec_succeeded"] is False
        assert result["code_source"] == "def f(): return WRONG"
        # Cache stays empty (nothing to cache).
        assert step._best_envelope is None


class TestRollbackModeIsolation:
    """Other modes (tdr, best_of_n) should NOT touch the rollback
    cache. Verify by exercising those modes and confirming
    _best_envelope stays None even on passing iterations."""

    @pytest.mark.parametrize("mode", ["tdr", "best_of_n"])
    def test_other_modes_do_not_use_cache(self, mode, tmp_path):
        """Build a step in the named mode and verify cache stays None."""
        import tempfile

        import yaml

        repo_root = Path(__file__).resolve().parents[1].parent
        tdr_steps_dir = (
            repo_root
            / "src"
            / "apecx_integration"
            / "composition"
            / "workflows"
            / "tdr_loop"
            / "steps"
        )

        cfg = {
            "class": "apecx_integration.composition.steps.tdr_iteration_step.TdrIterationStep",
            "name": f"step_{mode}",
            "description": "Test",
            "mode": mode,
            "writer_config_path": str(tdr_steps_dir / "code_writer.yml"),
            "executor_config_path": str(tdr_steps_dir / "executor.yml"),
            "input_data_units": {
                "in": {"class": "nanobrain.core.data_unit.DataUnitMemory", "name": "in"}
            },
            "output_data_units": {
                "out": {"class": "nanobrain.core.data_unit.DataUnitMemory", "name": "out"}
            },
            "triggers": [
                {
                    "class": "nanobrain.core.trigger.DataUnitChangeTrigger",
                    "data_unit": "in",
                }
            ],
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as f:
            yaml.safe_dump(cfg, f)
            path = f.name
        step = TdrIterationStep.from_config(path)
        assert step._mode == mode
        # The cache attribute exists but is never written to in
        # non-rollback modes.
        assert step._best_envelope is None

        async def fake_writer(input_data, **kwargs):
            return {"code_source": "def f(): return 1", "function_name_verified": "f"}

        async def fake_exec(input_data, **kwargs):
            return {
                "stdout": "",
                "stderr": "",
                "returncode": 0,
                "exec_succeeded": True,
                "elapsed_seconds": 0.01,
            }

        envelope = {
            "code_spec": "x",
            "function_name": "f",
            "test_code": "assert f() == 1",
        }

        with (
            mock.patch.object(step._writer, "process", side_effect=fake_writer),
            mock.patch.object(step._executor, "process", side_effect=fake_exec),
        ):
            asyncio.run(step.process(envelope))

        # Cache stays None — only tdr_with_rollback writes to it.
        assert step._best_envelope is None
