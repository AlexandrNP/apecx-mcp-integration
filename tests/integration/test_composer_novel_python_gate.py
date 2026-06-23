"""WS2b step 2: the composer AST-validates its novel Python before acceptance.

Before this, compose() only import-scanned novel code — a novel step that
imports clean but is structurally broken (syntax error, overrides execute(),
hallucinated nanobrain import) passed compose and failed only at workflow-run
time. Now those become `novel_python_invalid` WorkflowViolations that flow
through the existing C1 retry loop.

Tests _validate_or_raise directly (real validation, no LLM/mode dependency).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import apecx_integration
from apecx_integration.composition.composer import Composer, _RetryableValidationError

_CONFIG = Path(apecx_integration.__file__).parent / "composition" / "composer_config.yml"

# A minimal workflow_dict referencing a novel step "foo". The class path is
# intentionally bespoke (the novel step); other framework violations may
# co-occur — the tests assert ONLY on the novel_python_invalid rule.
_WD = {
    "name": "w",
    "steps": {"foo": {"class": "novelpkg.FooStep", "config": "steps/foo.yml"}},
    "links": {},
}


@pytest.fixture(scope="module")
def composer() -> Composer:
    return Composer.from_config(str(_CONFIG))


def _rule_ids(composer, novel_python) -> list[str]:
    try:
        composer._validate_or_raise(_WD, "yaml", {}, novel_python=novel_python)
        return []
    except _RetryableValidationError as exc:
        return [v.rule_id for v in exc.workflow_validation_error.violations]


def test_execute_override_in_novel_python_flagged(composer):
    broken = {
        "foo": "class FooStep(BaseStep):\n    async def execute(self, x):\n        return x\n"
    }
    assert "novel_python_invalid" in _rule_ids(composer, broken)


def test_syntax_error_in_novel_python_flagged(composer):
    broken = {"foo": "class FooStep(BaseStep):\n    async def process(self  bad syntax\n"}
    assert "novel_python_invalid" in _rule_ids(composer, broken)


def test_clean_novel_python_not_flagged(composer):
    clean = {
        "foo": (
            "from nanobrain.core.step import BaseStep\n"
            "class FooStep(BaseStep):\n"
            "    async def process(self, input_data, **kwargs):\n"
            "        return {}\n"
        )
    }
    # The bespoke class path may still trip step_class_unresolvable, but the
    # novel Python itself is clean => no novel_python_invalid.
    assert "novel_python_invalid" not in _rule_ids(composer, clean)


def test_no_novel_python_is_noop(composer):
    assert "novel_python_invalid" not in _rule_ids(composer, None)
    assert "novel_python_invalid" not in _rule_ids(composer, {})
