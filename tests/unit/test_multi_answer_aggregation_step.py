"""STEP-AGG — unit tests for MultiAnswerAggregationStep.

Pins:
  1. from_config loads the step + its wrapper YAML cleanly.
  2. Each of the four strategies produces the documented outcome
     on a known fixture.
  3. Empty candidate list raises a clear error (NOT silently emit
     empty string).
  4. Non-list input raises a clear error.
  5. Tie-breaking is by first-occurrence (deterministic).

No mocks — pure Python step; test against the real BaseStep
machinery via from_config.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps.multi_answer_aggregation_step import (
    MultiAnswerAggregationStep,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER_YAML = (
    REPO_ROOT
    / "src"
    / "apecx_integration"
    / "composition"
    / "workflows"
    / "violin_bvbrc"
    / "steps"
    / "multi_answer_aggregation.yml"
)


def _build(strategy: str = "most_frequent") -> MultiAnswerAggregationStep:
    """Construct the step from its shipped wrapper YAML, then
    swap the strategy via model_copy for per-test override.

    Going through ``from_config`` exercises the framework's
    mandatory creation pattern; ``model_copy`` is the supported way
    to vary a single field for tests without re-writing the YAML.
    """
    step = MultiAnswerAggregationStep.from_config(WRAPPER_YAML)
    # The framework stores the resolved component_config on the step;
    # poke the in-memory strategy so each test can vary it without
    # disk writes.
    step._strategy = strategy  # type: ignore[attr-defined]
    return step


def test_step_loads_via_from_config():
    step = MultiAnswerAggregationStep.from_config(WRAPPER_YAML)
    assert step is not None
    assert step.name == "multi_answer_aggregation"
    assert step._strategy == "most_frequent"  # type: ignore[attr-defined]


def test_most_frequent_picks_modal_candidate():
    step = _build("most_frequent")
    result = asyncio.run(step.process({"candidate_answers_input": ["A", "B", "A", "C", "A"]}))
    assert result == {"aggregated_answer_output": "A"}


def test_most_frequent_tie_broken_by_first_occurrence():
    """Counter.most_common(1) under Python 3.7+ dict ordering picks
    the first-occurrence candidate when frequencies tie. Pinning
    that contract so a future Python-internals change doesn't
    silently flip behavior."""
    step = _build("most_frequent")
    result = asyncio.run(step.process({"candidate_answers_input": ["B", "A", "A", "B"]}))
    # B and A each appear twice; B occurred first → B wins.
    assert result == {"aggregated_answer_output": "B"}


def test_longest_picks_longest_string():
    step = _build("longest")
    result = asyncio.run(
        step.process({"candidate_answers_input": ["short", "much longer answer", "medium"]})
    )
    assert result == {"aggregated_answer_output": "much longer answer"}


def test_first_passes_through_first_candidate():
    step = _build("first")
    result = asyncio.run(
        step.process({"candidate_answers_input": ["first one", "second", "third"]})
    )
    assert result == {"aggregated_answer_output": "first one"}


def test_concatenate_joins_with_separator():
    step = _build("concatenate")
    result = asyncio.run(step.process({"candidate_answers_input": ["one", "two", "three"]}))
    assert "one" in result["aggregated_answer_output"]
    assert "two" in result["aggregated_answer_output"]
    assert "three" in result["aggregated_answer_output"]


def test_empty_candidate_list_raises():
    """Empty input is an upstream-generation failure, not a step
    success. Per the EMPTY-FAIL discipline: surface it as a clear
    error, never silently emit ''."""
    step = _build("most_frequent")
    with pytest.raises(ValueError, match="empty"):
        asyncio.run(step.process({"candidate_answers_input": []}))


def test_missing_input_key_raises():
    step = _build("most_frequent")
    with pytest.raises(ValueError, match="candidate_answers_input"):
        asyncio.run(step.process({"some_other_key": []}))


def test_non_list_input_raises():
    step = _build("most_frequent")
    with pytest.raises(ValueError, match="must be a list"):
        asyncio.run(step.process({"candidate_answers_input": "not a list"}))


def test_non_string_candidates_are_stringified():
    """Robustness: if the upstream emits ints / dicts, we stringify
    rather than crash. Operators who want strict typing put a
    type-coercion step upstream."""
    step = _build("most_frequent")
    result = asyncio.run(step.process({"candidate_answers_input": [1, 2, 2, 3]}))
    assert result["aggregated_answer_output"] == "2"
