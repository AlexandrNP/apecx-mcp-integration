"""NN-1 — unit tests for FrameworkComplianceRunnerStep.

Tests pin:
1. Loads via from_config.
2. Empty code_source raises.
3. No framework classes → decision=pass (free-form code passes through).
4. Correct minimal BaseStep subclass → decision=pass.
5. from_config override at runtime → decision=fix with RuntimeError.
6. Hallucinated import → decision=fix with ImportError.
7. Output schema (decision, critique, passthrough).

Tests 4-6 actually exec a subprocess so they're slower than the AST
validator tests; aim for <5s each.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps.framework_compliance_runner_step import (
    FrameworkComplianceRunnerStep,
)


def _stage(tmp_path: Path) -> FrameworkComplianceRunnerStep:
    p = tmp_path / "v.yml"
    p.write_text("name: compliance_test\n")
    return FrameworkComplianceRunnerStep.from_config(str(p))


def test_loads(tmp_path):
    step = _stage(tmp_path)
    assert step.name == "compliance_test"


def test_empty_code_raises(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError, match="code_source"):
        asyncio.run(step.process({"code_source": "", "code_spec": "x", "entry_point": "f"}))


def test_no_framework_classes_passes(tmp_path):
    """Free-form code (no framework class to probe) should pass
    through. The runner has no opinion on MBPP-style code."""
    step = _stage(tmp_path)
    code = "def add(a, b): return a + b"
    out = asyncio.run(
        step.process({"code_source": code, "code_spec": "Write add", "entry_point": "add"})
    )
    assert out["decision"] == "pass"


@pytest.mark.integration
def test_correct_base_step_passes(tmp_path):
    """A correct minimal BaseStep subclass loads + runs cleanly."""
    step = _stage(tmp_path)
    code = (
        "from nanobrain.core.step import BaseStep\n"
        "class UpperStep(BaseStep):\n"
        "    async def process(self, input_data, **kwargs):\n"
        "        return {'output': input_data.get('text', '').upper()}\n"
    )
    out = asyncio.run(
        step.process(
            {"code_source": code, "code_spec": "Write UpperStep", "entry_point": "UpperStep"}
        )
    )
    assert out["decision"] == "pass", f"got critique: {out['critique']!r}"


@pytest.mark.integration
def test_from_config_override_caught_at_runtime(tmp_path):
    """from_config override surfaces as RuntimeError at probe time."""
    step = _stage(tmp_path)
    code = (
        "from nanobrain.core.step import BaseStep\n"
        "class BadStep(BaseStep):\n"
        "    @classmethod\n"
        "    def from_config(cls, p):\n"
        "        return cls(p)\n"
        "    async def process(self, input_data, **kwargs):\n"
        "        return {}\n"
    )
    out = asyncio.run(
        step.process({"code_source": code, "code_spec": "x", "entry_point": "BadStep"})
    )
    assert out["decision"] == "fix"
    assert "RuntimeError" in out["critique"]
    assert "BadStep" in out["critique"]


@pytest.mark.integration
def test_hallucinated_import_caught_at_runtime(tmp_path):
    """A non-existent nanobrain submodule import surfaces as
    ImportError when the compliance probe tries to load the module."""
    step = _stage(tmp_path)
    code = (
        "from nanobrain.does_not_exist import something\n"
        "from nanobrain.core.step import BaseStep\n"
        "class S(BaseStep):\n"
        "    async def process(self, input_data, **kwargs):\n"
        "        return {}\n"
    )
    out = asyncio.run(step.process({"code_source": code, "code_spec": "x", "entry_point": "S"}))
    assert out["decision"] == "fix"
    # The critique mentions the failure (either at import or at from_config).
    assert (
        "ImportError" in out["critique"]
        or "ModuleNotFoundError" in out["critique"]
        or "module" in out["critique"].lower()
    )


def test_output_schema(tmp_path):
    step = _stage(tmp_path)
    code = "def f(): return 1"
    out = asyncio.run(
        step.process(
            {
                "code_source": code,
                "code_spec": "Write f",
                "entry_point": "f",
                "test_hint": "assert f() == 1",
                "function_signature": "def f() -> int",
            }
        )
    )
    assert "decision" in out
    assert "code_source" in out
    assert "previous_attempt" in out
    assert "critique" in out
    assert out["code_spec"] == "Write f"
    assert out["entry_point"] == "f"
