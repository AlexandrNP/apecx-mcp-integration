"""T07 estimator tests — pure-function coverage.

Intentionally kept in ``tests/unit/`` rather than
``tests/integration/``: the estimator is a pure function with no
external dependencies, no DB access, no HTTP. Unit tests are the
right bucket.

Per workspace CLAUDE.md mocks-carve-out: unit tests of a pure
transformation are explicitly allowed, no integration-test pairing
required (the function HAS no external dependency to mock).
"""

from __future__ import annotations

import pytest

from apecx_integration.control_plane.accounting.cost_estimator import (
    estimate_workflow_cost,
    estimated_wall_time_seconds,
)

# ---------------------------------------------------------------------------
# Core heuristic
# ---------------------------------------------------------------------------

def test_empty_steps_block_produces_zero_total():
    result = estimate_workflow_cost({"steps": {}})
    assert result.total_core_hours == 0.0
    assert result.per_step_core_hours == {}
    assert result.endpoint == "local"


def test_explicit_estimated_core_hours_is_honored():
    """If a step author provides the hint, trust it."""
    wf = {
        "steps": {
            "big_step": {
                "class": "some.module.BigStep",
                "estimated_core_hours": 42.0,
            }
        }
    }
    result = estimate_workflow_cost(wf)
    assert result.per_step_core_hours == {"big_step": 42.0}
    assert result.total_core_hours == 42.0


def test_llm_class_gets_llm_default():
    """Any step class with 'LLM' / 'Agent' / 'Ollama' in the path gets
    the LLM-ish default (0.05 core-hours)."""
    wf = {
        "steps": {
            "extract": {"class": "apecx_integration.composition.steps.db_integration_wrappers.EntityExtractionStep"},
            "ollama_agent": {"class": "nanobrain.core.agent.SimpleAgent"},  # matches 'Agent'
        }
    }
    result = estimate_workflow_cost(wf)
    # 'EntityExtractionStep' — does not contain LLM/Agent/Ollama as substrings;
    # falls through to generic 0.1.
    assert result.per_step_core_hours["extract"] == 0.1
    # 'SimpleAgent' contains 'Agent' → 0.05.
    assert result.per_step_core_hours["ollama_agent"] == 0.05


def test_snapshot_and_file_reader_get_cheap_default():
    wf = {
        "steps": {
            "snap": {"class": "apecx_integration.BVBRCSnapshotTool"},
            "read": {"class": "apecx_integration.composition.steps.file_readers.DelimitedFileReaderStep"},
        }
    }
    result = estimate_workflow_cost(wf)
    assert result.per_step_core_hours["snap"] == 0.01
    assert result.per_step_core_hours["read"] == 0.01


def test_generic_fallback_for_unclassified_step():
    wf = {"steps": {"mystery": {"class": "some.random.Widget"}}}
    result = estimate_workflow_cost(wf)
    assert result.per_step_core_hours["mystery"] == 0.1


def test_mixed_workflow_sum():
    wf = {
        "steps": {
            "reader": {"class": "FileReaderStep"},           # 0.01
            "agent":  {"class": "SimpleAgent"},              # 0.05
            "llm":    {"class": "MyLLMStep"},                # 0.05
            "custom": {"class": "Widget", "estimated_core_hours": 1.5},
            "misc":   {"class": "OtherStep"},                # 0.1 fallback
        }
    }
    result = estimate_workflow_cost(wf)
    expected = 0.01 + 0.05 + 0.05 + 1.5 + 0.1
    assert result.total_core_hours == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Confidence interval
# ---------------------------------------------------------------------------

def test_confidence_interval_brackets_total_with_wide_band():
    wf = {"steps": {"s": {"class": "X", "estimated_core_hours": 10.0}}}
    result = estimate_workflow_cost(wf)
    low, high = result.confidence_interval
    assert low == pytest.approx(3.0)   # 0.3 × 10
    assert high == pytest.approx(30.0)  # 3.0 × 10
    # The wide-band is intentional — AP §5.7 "not about accuracy."
    assert high / low == pytest.approx(10.0)


def test_confidence_interval_is_zero_when_total_zero():
    result = estimate_workflow_cost({"steps": {}})
    assert result.confidence_interval == (0.0, 0.0)


# ---------------------------------------------------------------------------
# Endpoint passthrough (pricing is future work)
# ---------------------------------------------------------------------------

def test_endpoint_name_passes_through_unchanged():
    result = estimate_workflow_cost({"steps": {}}, endpoint="alcf_polaris")
    assert result.endpoint == "alcf_polaris"


def test_endpoint_factor_is_1_regardless_of_name():
    """Per-endpoint pricing is not yet implemented; factor stays 1.0."""
    wf = {"steps": {"s": {"class": "X", "estimated_core_hours": 1.0}}}
    local = estimate_workflow_cost(wf, endpoint="local")
    alcf = estimate_workflow_cost(wf, endpoint="alcf_polaris")
    assert local.total_core_hours == alcf.total_core_hours == 1.0


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def test_missing_steps_block_raises():
    with pytest.raises(ValueError, match="'steps:' mapping"):
        estimate_workflow_cost({"name": "nope"})


def test_steps_not_mapping_raises():
    with pytest.raises(ValueError, match="'steps:' mapping"):
        estimate_workflow_cost({"steps": [1, 2, 3]})


def test_malformed_step_entry_is_skipped_not_raised():
    """A step whose value is e.g. a string rather than a dict gets
    skipped by the estimator — the framework loader will catch it
    separately. Don't raise twice."""
    wf = {
        "steps": {
            "good": {"class": "X", "estimated_core_hours": 1.0},
            "bad":  "this isn't a dict",
        }
    }
    result = estimate_workflow_cost(wf)
    assert list(result.per_step_core_hours.keys()) == ["good"]
    assert result.total_core_hours == 1.0


# ---------------------------------------------------------------------------
# Wall-time helper
# ---------------------------------------------------------------------------

def test_wall_time_seconds_assumes_sequential():
    assert estimated_wall_time_seconds(0.0) == 0.0
    assert estimated_wall_time_seconds(1.0) == 3600.0
    assert estimated_wall_time_seconds(0.05) == 180.0  # 3 minutes


def test_wall_time_is_upper_bound_documentation():
    """The helper assumes sequential execution; with any parallelism
    actual wall time is ≤ this value. This test just pins the
    contract so a future change doesn't silently switch to a different
    assumption.
    """
    hours = 10.0
    wt = estimated_wall_time_seconds(hours)
    assert wt == hours * 3600.0
