"""CW-4 — unit tests for IsolatedPyExecStep.

Pins:
  1. Refuse-by-default: without APECX_CODE_EXEC=1 → RuntimeError.
  2. With env enabled: empty code_source raises (EMPTY-FAIL).
  3. With env enabled: unparseable code raises (AST gate before subprocess).
  4. With env enabled: simple script runs, returns structured result.
  5. With env enabled: assertion failure → returncode != 0, exec_succeeded=False
     (does NOT raise unless raise_on_nonzero_returncode=True).
  6. With env enabled: timeout fires within timeout_seconds + budget, exec_succeeded=False.
  7. Subprocess does NOT inherit parent env (sensitive vars scrubbed).
  8. ``extra_env`` reintroduces variables when needed.
  9. raise_on_nonzero_returncode=True converts a non-zero exit to RuntimeError.
 10. Invalid entrypoint identifier raises.

These tests EXECUTE real Python subprocesses — they take ~1-2s each.
The mocks-carve-out applies here: the unit tests pin the wrapper
contract via real subprocess interactions (no Popen mock).
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from apecx_integration.composition.steps.isolated_py_exec_step import (
    _ENV_GATE,
    IsolatedPyExecStep,
)


def _stage_step(tmp_path: Path, *, yaml_extras: str = "") -> IsolatedPyExecStep:
    body = "name: exec_test\n" + yaml_extras
    p = tmp_path / "exec.yml"
    p.write_text(body)
    return IsolatedPyExecStep.from_config(str(p))


# ---------------------------------------------------------------------------
# 1. Refuse-by-default
# ---------------------------------------------------------------------------


def test_refuses_without_env_opt_in(tmp_path, monkeypatch):
    """No APECX_CODE_EXEC=1 → step refuses immediately. Note: we
    delete the var defensively in case the dev env has it set."""
    monkeypatch.delenv(_ENV_GATE, raising=False)
    step = _stage_step(tmp_path)
    with pytest.raises(RuntimeError, match="refused to execute"):
        asyncio.run(step.process({"code_source": "x = 1"}))


# ---------------------------------------------------------------------------
# Tests below require the env opt-in.
# ---------------------------------------------------------------------------


@pytest.fixture
def enabled_env(monkeypatch):
    monkeypatch.setenv(_ENV_GATE, "1")
    return monkeypatch


def test_empty_code_source_raises(tmp_path, enabled_env):
    step = _stage_step(tmp_path)
    with pytest.raises(ValueError, match="code_source"):
        asyncio.run(step.process({"code_source": "   "}))


def test_unparseable_code_raises_before_subprocess(tmp_path, enabled_env):
    """AST gate fires before subprocess spawn — the error message
    must reference Python validity, not subprocess return code."""
    step = _stage_step(tmp_path)
    with pytest.raises(ValueError, match="not valid Python"):
        asyncio.run(step.process({"code_source": "def (oops"}))


def test_simple_script_runs_and_returns_structured_result(tmp_path, enabled_env):
    step = _stage_step(tmp_path)
    result = asyncio.run(step.process({"code_source": "print('hello from subprocess')"}))
    assert result["returncode"] == 0
    assert result["exec_succeeded"] is True
    assert "hello from subprocess" in result["stdout"]
    assert result["stderr"] == ""
    assert result["elapsed_seconds"] > 0


def test_function_with_assertion_success(tmp_path, enabled_env):
    step = _stage_step(tmp_path)
    result = asyncio.run(
        step.process(
            {
                "code_source": "def add(a, b):\n    return a + b\n",
                "test_code": "assert add(2, 3) == 5",
            }
        )
    )
    assert result["exec_succeeded"] is True
    assert result["returncode"] == 0


def test_assertion_failure_reports_nonzero_returncode_without_raising(tmp_path, enabled_env):
    """AssertionError → returncode != 0. The step REPORTS the failure
    rather than raising, so downstream review/retry steps can act."""
    step = _stage_step(tmp_path)
    result = asyncio.run(
        step.process(
            {
                "code_source": "def add(a, b):\n    return a + b\n",
                "test_code": "assert add(2, 3) == 999",
            }
        )
    )
    assert result["exec_succeeded"] is False
    assert result["returncode"] != 0
    assert "AssertionError" in result["stderr"]


def test_timeout_kills_subprocess_and_marks_failure(tmp_path, enabled_env):
    step = _stage_step(tmp_path, yaml_extras="timeout_seconds: 0.5\n")
    start = time.monotonic()
    result = asyncio.run(
        step.process({"code_source": "import time\nwhile True:\n    time.sleep(0.01)"})
    )
    elapsed = time.monotonic() - start
    # Killed within the timeout + reasonable cleanup budget.
    assert elapsed < 3.0, f"timeout did not fire in time: {elapsed:.2f}s"
    assert result["exec_succeeded"] is False
    assert result["returncode"] == -1
    assert "timeout" in result["stderr"].lower()


def test_subprocess_does_not_inherit_secret_env(tmp_path, enabled_env):
    """The subprocess gets a scrubbed env — APECX_LLM_API_KEY etc. are
    NOT visible. We set a synthetic secret on the parent and verify
    the child can't see it."""
    enabled_env.setenv("APECX_CW_TEST_SECRET", "should-not-leak")
    step = _stage_step(tmp_path)
    result = asyncio.run(
        step.process(
            {
                "code_source": (
                    "import os\n"
                    "secret = os.environ.get('APECX_CW_TEST_SECRET', '<missing>')\n"
                    "print(secret)"
                )
            }
        )
    )
    assert result["exec_succeeded"] is True
    assert "<missing>" in result["stdout"]
    assert "should-not-leak" not in result["stdout"]


def test_extra_env_reintroduces_variables(tmp_path, enabled_env):
    step = _stage_step(
        tmp_path,
        yaml_extras=("extra_env:\n  APECX_CW_TEST_VAR: visible-to-child\n"),
    )
    result = asyncio.run(
        step.process(
            {"code_source": ("import os\nprint(os.environ.get('APECX_CW_TEST_VAR', '<missing>'))")}
        )
    )
    assert "visible-to-child" in result["stdout"]


def test_raise_on_nonzero_returncode_converts_failure_to_exception(tmp_path, enabled_env):
    step = _stage_step(tmp_path, yaml_extras="raise_on_nonzero_returncode: true\n")
    with pytest.raises(RuntimeError, match="non-zero exit"):
        asyncio.run(step.process({"code_source": "import sys\nsys.exit(7)"}))


def test_entrypoint_invokes_function_when_no_test_code(tmp_path, enabled_env):
    step = _stage_step(tmp_path)
    result = asyncio.run(
        step.process(
            {
                "code_source": "def greeting():\n    return 'hi from entrypoint'\n",
                "entrypoint": "greeting",
            }
        )
    )
    assert result["exec_succeeded"] is True
    assert "hi from entrypoint" in result["stdout"]


def test_invalid_entrypoint_identifier_raises(tmp_path, enabled_env):
    step = _stage_step(tmp_path)
    with pytest.raises(ValueError, match="not a valid Python identifier"):
        asyncio.run(
            step.process(
                {
                    "code_source": "def foo():\n    return 1\n",
                    "entrypoint": "1bad name; rm -rf /",
                }
            )
        )


def test_python_executable_defaults_to_sys_executable(tmp_path, enabled_env):
    step = _stage_step(tmp_path)
    result = asyncio.run(step.process({"code_source": "import sys\nprint(sys.executable)"}))
    assert result["exec_succeeded"] is True
    # The child reports the same executable the parent uses.
    assert sys.executable in result["stdout"]
