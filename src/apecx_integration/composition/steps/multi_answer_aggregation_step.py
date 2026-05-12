"""STEP-AGG — self-consistency aggregation step.

The "self-consistency" pattern (Wang et al. 2022; surveyed in 2026
agentic-RAG literature) generates N independent answers to a query
and aggregates them — typically by voting on the most-frequent or
selecting the longest/most-detailed answer. Used to reduce LLM
variance on tasks where a single sample is unreliable.

The N-fold generation lives in upstream steps (e.g., N independent
``RagSynthesisStep`` invocations fanned out via async). This step
consumes the resulting list and aggregates deterministically.

Why deterministic Python here (not another LLM call):
  - Aggregation is a counting / picking problem, not a generation
    problem. An LLM would add cost, variance, and a hallucination
    surface for zero leverage.
  - Pure-Python means no LLM round-trip; the step's wall time is
    bounded by len(candidates).
  - Operators can audit + diff verdicts; an LLM aggregator would
    be a black box.

Framework compliance:
  - Subclasses ``BaseStep``; implements ``process()``; never
    overrides ``execute()``.
  - Config schema extends ``StepConfig`` with a single
    ``aggregation_strategy`` field validated to a fixed enum.
  - Inline ``config: { ... }`` is fine here (DataUnit/Link/Trigger
    inputs are inline-eligible; this step's wrapper YAML uses a
    file path per the framework rule).

Shipped as the first realized component of the SKEL-PLUS deferred
patterns. Pairs with the ``self_consistency_synthesis`` skeleton
in ``composition/skeletons/``.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Literal

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import Field

log = logging.getLogger(__name__)


AggregationStrategy = Literal["most_frequent", "longest", "first", "concatenate"]


class MultiAnswerAggregationStepConfig(StepConfig):
    """Config for ``MultiAnswerAggregationStep``.

    ``aggregation_strategy`` is the only knob:
      - ``most_frequent`` (default): canonical self-consistency.
        Counts identical candidates; emits the most-frequent. Ties
        broken by first-occurrence order.
      - ``longest``: pick the longest candidate by character count.
        Useful when "more detail = better".
      - ``first``: pass through the first candidate (no aggregation;
        useful for A/B harnesses).
      - ``concatenate``: join all candidates with a separator. Use
        when downstream consumers want every candidate visible.
    """

    aggregation_strategy: AggregationStrategy = Field(
        default="most_frequent",
        description=(
            "How to aggregate the candidate list. 'most_frequent' is "
            "canonical self-consistency; 'longest' for verbosity-as-quality "
            "heuristic; 'first' to disable aggregation in A/B tests; "
            "'concatenate' to surface every candidate downstream."
        ),
    )
    concatenate_separator: str = Field(
        default="\n\n---\n\n",
        description=(
            "Separator used by the 'concatenate' strategy. Default is a "
            "markdown horizontal-rule pattern so the joined output renders "
            "as clearly-delimited blocks in downstream reviewers."
        ),
    )


class MultiAnswerAggregationStep(BaseStep):
    """Aggregate a list of candidate answers into a single answer.

    Expected ``process()`` input::

        {"candidate_answers_input": ["answer A", "answer B", "answer A"]}

    Return shape::

        {"aggregated_answer_output": "answer A"}

    Pure Python, no LLM call. Idempotent + deterministic given the
    same input list (modulo tie-breaking on first occurrence).
    """

    COMPONENT_TYPE: str = "multi_answer_aggregation_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name", "aggregation_strategy"]

    @classmethod
    def _get_config_class(cls):
        return MultiAnswerAggregationStepConfig

    @classmethod
    def extract_component_config(cls, config: MultiAnswerAggregationStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "aggregation_strategy": config.aggregation_strategy,
            "concatenate_separator": config.concatenate_separator,
        }

    def _init_from_config(
        self,
        config: MultiAnswerAggregationStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._strategy: AggregationStrategy = component_config["aggregation_strategy"]
        self._separator: str = component_config["concatenate_separator"]

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        candidates = input_data.get("candidate_answers_input")
        if candidates is None:
            raise ValueError(
                f"MultiAnswerAggregationStep '{self.name}': input_data "
                "must contain 'candidate_answers_input'; got "
                f"keys={sorted(input_data) if isinstance(input_data, dict) else type(input_data).__name__}"
            )
        if not isinstance(candidates, list):
            raise ValueError(
                f"MultiAnswerAggregationStep '{self.name}': "
                f"'candidate_answers_input' must be a list; got "
                f"{type(candidates).__name__}"
            )
        if not candidates:
            # Empty input is a real failure for this step: the upstream
            # produced no candidates. Per the EMPTY-FAIL discipline we
            # surface this as a clear error rather than silently emit
            # an empty string. Callers that want to tolerate emptiness
            # do so upstream.
            raise ValueError(
                f"MultiAnswerAggregationStep '{self.name}': candidate "
                "list is empty — no aggregation possible. Upstream "
                "generation step produced no candidates."
            )
        # Defensive: stringify each candidate for the counting / picking
        # heuristics. Operators who need richer aggregation (e.g.,
        # dict-shaped answers) author a different step.
        candidates_str = [self._stringify(c) for c in candidates]
        aggregated = self._aggregate(candidates_str)
        log.info(
            "MultiAnswerAggregationStep '%s': strategy=%s n=%d → len=%d",
            self.name,
            self._strategy,
            len(candidates_str),
            len(aggregated),
        )
        return {"aggregated_answer_output": aggregated}

    def _aggregate(self, candidates: list[str]) -> str:
        if self._strategy == "first":
            return candidates[0]
        if self._strategy == "longest":
            return max(candidates, key=len)
        if self._strategy == "concatenate":
            return self._separator.join(candidates)
        # most_frequent — default.
        counts = Counter(candidates)
        # ``Counter.most_common(1)`` returns the highest count; ties
        # are broken by insertion order (Python 3.7+ dict ordering),
        # which mirrors first-occurrence for our list.
        most_common, _ = counts.most_common(1)[0]
        return most_common

    @staticmethod
    def _stringify(candidate: Any) -> str:
        if isinstance(candidate, str):
            return candidate
        if candidate is None:
            return ""
        return str(candidate)


__all__ = [
    "AggregationStrategy",
    "MultiAnswerAggregationStep",
    "MultiAnswerAggregationStepConfig",
]
