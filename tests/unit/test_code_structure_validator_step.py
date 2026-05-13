"""CGU-P2-T1b — unit tests for CodeStructureValidatorStep.

The validator is deterministic (no LLM). Tests pin:

1. Loads via from_config.
2. Empty code_source raises ValueError (cannot validate nothing).
3. SyntaxError surfaces as decision=fix + line-number critique.
4. Missing entry_point surfaces as decision=fix.
5. from_config override on a BaseStep subclass is caught.
6. execute override on a BaseStep subclass is caught.
7. Hallucinated nanobrain.* submodule import is caught.
8. Correct minimal subclass passes (decision=pass, critique=PASS).
9. Trigger-envelope unwrap works.
10. Output schema: decision, critique, previous_attempt, passthrough.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps.code_structure_validator_step import (
    CodeStructureValidatorStep,
)


def _stage(tmp_path: Path, *, yaml_extras: str = "") -> CodeStructureValidatorStep:
    body = "name: validator_test\n" + yaml_extras
    p = tmp_path / "v.yml"
    p.write_text(body)
    return CodeStructureValidatorStep.from_config(str(p))


def test_loads_with_defaults(tmp_path):
    step = _stage(tmp_path)
    assert step.name == "validator_test"


def test_empty_code_source_raises(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError, match="code_source"):
        asyncio.run(step.process({"code_source": "", "code_spec": "x", "entry_point": "f"}))


def test_syntax_error_routes_to_fix(tmp_path):
    step = _stage(tmp_path)
    bad = "def f( :\n    return 1\n"
    out = asyncio.run(step.process({"code_source": bad, "code_spec": "f", "entry_point": "f"}))
    assert out["decision"] == "fix"
    assert "SyntaxError" in out["critique"]


def test_missing_entry_point_routes_to_fix(tmp_path):
    step = _stage(tmp_path)
    code = "def g(): return 1"
    out = asyncio.run(step.process({"code_source": code, "code_spec": "f", "entry_point": "f"}))
    assert out["decision"] == "fix"
    assert "entry point ``f``" in out["critique"]


def test_from_config_override_caught(tmp_path):
    step = _stage(tmp_path)
    code = (
        "from nanobrain.core.step import BaseStep\n"
        "class MyStep(BaseStep):\n"
        "    COMPONENT_TYPE = 'x'\n"
        "    @classmethod\n"
        "    def from_config(cls, p):\n"
        "        return cls(p)\n"
        "    async def process(self, input_data, **kwargs):\n"
        "        return {}\n"
    )
    out = asyncio.run(
        step.process({"code_source": code, "code_spec": "x", "entry_point": "MyStep"})
    )
    assert out["decision"] == "fix"
    assert "from_config" in out["critique"]
    assert "MyStep" in out["critique"]


def test_execute_override_caught(tmp_path):
    step = _stage(tmp_path)
    code = (
        "from nanobrain.core.step import BaseStep\n"
        "class MyStep(BaseStep):\n"
        "    COMPONENT_TYPE = 'x'\n"
        "    async def execute(self, input_data):\n"
        "        return {}\n"
    )
    out = asyncio.run(
        step.process({"code_source": code, "code_spec": "x", "entry_point": "MyStep"})
    )
    assert out["decision"] == "fix"
    assert "execute" in out["critique"]


def test_hallucinated_import_caught(tmp_path):
    step = _stage(tmp_path)
    code = (
        "from nanobrain.utils import helper\n"
        "from nanobrain.core.step import BaseStep\n"
        "class MyStep(BaseStep):\n"
        "    async def process(self, input_data, **kwargs):\n"
        "        return {}\n"
    )
    out = asyncio.run(
        step.process({"code_source": code, "code_spec": "x", "entry_point": "MyStep"})
    )
    assert out["decision"] == "fix"
    assert "nanobrain.utils" in out["critique"]


def test_correct_minimal_subclass_passes(tmp_path):
    step = _stage(tmp_path)
    code = (
        "from nanobrain.core.step import BaseStep\n"
        "class UpperStep(BaseStep):\n"
        "    COMPONENT_TYPE = 'upper'\n"
        "    async def process(self, input_data, **kwargs):\n"
        "        return {'output': input_data['text'].upper()}\n"
    )
    out = asyncio.run(
        step.process(
            {"code_source": code, "code_spec": "Write UpperStep", "entry_point": "UpperStep"}
        )
    )
    assert out["decision"] == "pass", f"got {out['critique']!r}"
    assert out["critique"] == "PASS"


def test_trigger_envelope_unwrap(tmp_path):
    step = _stage(tmp_path)
    code = "def f(): return 1"
    out = asyncio.run(
        step.process(
            {"validator_input": {"code_source": code, "code_spec": "f", "entry_point": "f"}}
        )
    )
    # f IS defined here so this should PASS (no class involved).
    assert out["decision"] == "pass"


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
    assert out["test_hint"] == "assert f() == 1"
    assert out["function_signature"] == "def f() -> int"
    assert out["previous_attempt"] == code
