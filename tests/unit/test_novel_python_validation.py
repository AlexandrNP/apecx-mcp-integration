"""Direct tests for the shared validate_python_structure (extracted from
CodeStructureValidatorStep so the composer can reuse it). Real AST over real
code strings — no mocks, deterministic, no exec."""

from __future__ import annotations

from apecx_integration.composition.novel_python_validation import (
    validate_python_structure,
)


def test_clean_step_passes():
    code = (
        "from nanobrain.core.step import BaseStep\n"
        "class Foo(BaseStep):\n"
        "    async def process(self, input_data, **kwargs):\n"
        "        return {}\n"
    )
    assert validate_python_structure(code, "Foo") == []


def test_syntax_error_returns_single_issue():
    issues = validate_python_structure("def f(:\n    pass\n")
    assert len(issues) == 1 and "SyntaxError" in issues[0]


def test_execute_override_flagged():
    code = "class Foo(BaseStep):\n    async def execute(self, x):\n        return x\n"
    assert any("execute" in i for i in validate_python_structure(code, "Foo"))


def test_from_config_override_flagged():
    code = (
        "class Foo(BaseStep):\n"
        "    @classmethod\n"
        "    def from_config(cls, c):\n"
        "        return cls()\n"
    )
    assert any("from_config" in i for i in validate_python_structure(code, "Foo"))


def test_hallucinated_nanobrain_import_flagged():
    code = (
        "from nanobrain.utils import thing\n"
        "class Foo(BaseStep):\n"
        "    async def process(self, d, **k):\n"
        "        return {}\n"
    )
    assert any(
        "non-existent nanobrain submodule" in i for i in validate_python_structure(code, "Foo")
    )


def test_missing_entry_point_flagged():
    assert any(
        "entry point" in i for i in validate_python_structure("class Bar:\n    pass\n", "Foo")
    )


def test_real_core_submodule_import_not_flagged():
    # Regression: a leaf whitelist false-flagged real nanobrain.core.* submodules
    # at compose time. Roots accept every real core submodule.
    code = (
        "from nanobrain.core.component_base import FromConfigBase\n"
        "from nanobrain.core.step import BaseStep\n"
        "class Foo(BaseStep):\n"
        "    async def process(self, d, **k):\n"
        "        return {}\n"
    )
    assert validate_python_structure(code, "Foo") == []


def test_non_framework_class_not_checked_for_overrides():
    # execute() on a NON-framework class is fine (only framework-base subclasses
    # are checked) — guards against false positives.
    code = "class Plain:\n    def execute(self):\n        return 1\n"
    assert validate_python_structure(code, "Plain") == []
