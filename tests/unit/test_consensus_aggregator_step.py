"""Unit tests for ConsensusAggregatorStep (deterministic voter)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps.consensus_aggregator_step import (
    ConsensusAggregatorStep,
)


def _stage(tmp_path: Path, *, yaml_extras: str = "") -> ConsensusAggregatorStep:
    p = tmp_path / "v.yml"
    p.write_text("name: aggregator_test\n" + yaml_extras)
    return ConsensusAggregatorStep.from_config(str(p))


def test_loads(tmp_path):
    step = _stage(tmp_path)
    assert step.name == "aggregator_test"


def test_empty_candidates_raises(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError, match="empty candidates"):
        asyncio.run(step.process({"candidates": [], "code_spec": "x"}))


def test_unknown_strategy_rejected(tmp_path):
    with pytest.raises(Exception, match="(?i)voting_strategy"):
        _stage(tmp_path, yaml_extras="voting_strategy: badstrategy\n")


def test_first_non_empty_strategy_picks_first(tmp_path):
    """first_non_empty marks all non-empty candidates as PASS; the
    aggregator picks the FIRST one (deterministic; index-order)."""
    step = _stage(tmp_path, yaml_extras="voting_strategy: first_non_empty\n")
    out = asyncio.run(
        step.process(
            {
                "candidates": [
                    {"code_source": "def a(): return 1"},
                    {"code_source": "def b(): return 1\n# longer body"},
                ],
                "code_spec": "x",
                "entry_point": "a",
            }
        )
    )
    assert out["code_source"].startswith("def a")
    assert out["winning_index"] == 0


def test_first_non_empty_skips_empty_candidates(tmp_path):
    """An empty-string candidate is scored as fail (issue_count=99)
    so it gets sorted behind the non-empty pass'd ones."""
    step = _stage(tmp_path, yaml_extras="voting_strategy: first_non_empty\n")
    out = asyncio.run(
        step.process(
            {
                "candidates": [
                    {"code_source": ""},
                    {"code_source": "def real(): return 1"},
                ],
                "code_spec": "x",
                "entry_point": "real",
            }
        )
    )
    assert "def real" in out["code_source"]


def test_ast_validator_picks_first_pass(tmp_path):
    """With voting_strategy=ast_validator, the first candidate that
    has no AST issues wins. Here candidate 1 has a from_config
    override; candidate 2 is clean."""
    step = _stage(tmp_path, yaml_extras="voting_strategy: ast_validator\n")
    bad = (
        "from nanobrain.core.step import BaseStep\n"
        "class S(BaseStep):\n"
        "    @classmethod\n"
        "    def from_config(cls, p): return cls(p)\n"
        "    async def process(self, input_data, **kwargs):\n"
        "        return {}\n"
    )
    good = (
        "from nanobrain.core.step import BaseStep\n"
        "class S(BaseStep):\n"
        "    async def process(self, input_data, **kwargs):\n"
        "        return {}\n"
    )
    out = asyncio.run(
        step.process(
            {
                "candidates": [{"code_source": bad}, {"code_source": good}],
                "code_spec": "x",
                "entry_point": "S",
            }
        )
    )
    assert out["winning_index"] == 1
    assert out["voted_passes"] == 1


def test_no_passes_falls_back_to_best_failing(tmp_path):
    """When no candidate passes, pick the one with fewest issues."""
    step = _stage(tmp_path, yaml_extras="voting_strategy: ast_validator\n")
    # Two bad candidates: first has both from_config + execute overrides
    # (2 issues), second has only from_config (1 issue).
    worst = (
        "from nanobrain.core.step import BaseStep\n"
        "class S(BaseStep):\n"
        "    @classmethod\n"
        "    def from_config(cls, p): return cls(p)\n"
        "    async def execute(self, x): return {}\n"
        "    async def process(self, input_data, **kwargs):\n"
        "        return {}\n"
    )
    less_bad = (
        "from nanobrain.core.step import BaseStep\n"
        "class S(BaseStep):\n"
        "    @classmethod\n"
        "    def from_config(cls, p): return cls(p)\n"
        "    async def process(self, input_data, **kwargs):\n"
        "        return {}\n"
    )
    out = asyncio.run(
        step.process(
            {
                "candidates": [{"code_source": worst}, {"code_source": less_bad}],
                "code_spec": "x",
                "entry_point": "S",
            }
        )
    )
    assert out["voted_passes"] == 0
    assert out["winning_index"] == 1, "should pick the less-bad one"


def test_single_candidate_passthrough(tmp_path):
    """When upstream is a single-shot drafter (not multi-sample), the
    aggregator receives a single code_source instead of a list; it
    wraps + handles gracefully."""
    step = _stage(tmp_path)
    out = asyncio.run(
        step.process(
            {
                "code_source": "def f(): return 1",
                "code_spec": "Write f",
                "entry_point": "f",
            }
        )
    )
    assert out["code_source"] == "def f(): return 1"
    assert out["n_samples"] == 1


def test_output_schema(tmp_path):
    step = _stage(tmp_path)
    out = asyncio.run(
        step.process(
            {
                "candidates": [{"code_source": "def f(): return 1"}],
                "code_spec": "Write f",
                "entry_point": "f",
            }
        )
    )
    assert "code_source" in out
    assert "winning_index" in out
    assert "voted_passes" in out
    assert "n_samples" in out
    assert "voting_strategy" in out
    assert out["code_spec"] == "Write f"
