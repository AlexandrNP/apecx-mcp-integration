"""REVIEW-AGENT — semantic-fit reviewer for composer-generated workflows.

The composer's existing machinery (A1 validator, CPR resolver, spec
expander, C1 retry, EMPTY-FAIL gate) catches every STRUCTURAL failure
shape we know about. None of them catch SEMANTIC mismatch: the LLM
producing a workflow that loads cleanly + runs to RUN_COMPLETED but
solves the wrong task (e.g., spec-mode session 7 picked synthesis
steps for a pathogen prompt; the workflow ran, produced no useful
output, marked COMPLETED).

The reviewer is a second-pass agent that asks one question of the
generated workflow: "does this semantically address the user's task?"
It emits a structured verdict the composer can act on — approve and
return, or reject and retry with the structured concerns as feedback.

Framework-native packaging:
  - Reviewer uses the SAME ``_llm_factory`` the composer uses, so
    operators can swap models per-deployment via the existing
    APECX_LLM_* env vars. A future iteration can wrap as
    ``nanobrain.core.agent.SimpleAgent.from_config(reviewer.yml)``
    without changing the public interface (`WorkflowReviewer.review`).
  - The reviewer's system prompt lives at
    ``composer_prompts/reviewer_system.md`` — same load discipline
    as the composer's other prompts.

The reviewer is OPT-IN. Default off because:
  - Adds an LLM round-trip per compose (~30-60s on mistral-nemo).
  - Useful when adoption is at stake; redundant when an operator is
    running a curated skeleton set.
Enable via ``APECX_COMPOSER_REVIEW=1`` env var or
``composer_mode``-config field flip.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class ReviewVerdict:
    """One reviewer judgment over a composed workflow.

    Fields:
        approved: True = the workflow plausibly addresses the
            user's task; False = retry with feedback.
        reasoning: human-readable verdict (1-3 sentences).
        concerns: structured list of issues the LLM can act on in a
            retry. Non-empty even when ``approved=True`` for
            non-blocking observations.
        review_used: True when an actual LLM call happened; False
            when review was disabled or short-circuited (e.g., the
            LLM responded with malformed output and we chose to
            pass through rather than block).
        raw_response: the LLM's verbatim response, kept for audit.
    """

    approved: bool
    reasoning: str
    concerns: tuple[str, ...] = field(default_factory=tuple)
    review_used: bool = True
    raw_response: str = ""


class WorkflowReviewer:
    """Second-pass semantic reviewer for composer output.

    Construction takes the same ``llm_factory`` callable the
    composer uses (so the reviewer can run on the same Ollama,
    same auth, same temperature controls) plus the reviewer system
    prompt body.

    Public surface:
        ``async review(user_prompt, yaml_text, summary_sentence) -> ReviewVerdict``

    Operators who want a stronger / different model for review can
    inject a different factory at composer construction time.
    """

    def __init__(
        self,
        *,
        llm_factory,
        system_prompt: str,
        model: str = "mistral-nemo:latest",
        base_url: str = "http://localhost:11434/v1",
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> None:
        self._llm_factory = llm_factory
        self._system_prompt = system_prompt
        self._model = model
        self._base_url = base_url
        self._temperature = temperature
        self._max_tokens = max_tokens

    @classmethod
    def from_prompt_dir(
        cls,
        prompt_dir: Path,
        *,
        llm_factory,
        model: str = "mistral-nemo:latest",
        base_url: str = "http://localhost:11434/v1",
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> WorkflowReviewer | None:
        """Build a reviewer when the prompt file exists; return
        ``None`` when it doesn't (operator hasn't shipped it). The
        composer treats ``None`` as "review disabled."
        """
        prompt_path = prompt_dir / "reviewer_system.md"
        if not prompt_path.is_file():
            return None
        body = prompt_path.read_text(encoding="utf-8")
        return cls(
            llm_factory=llm_factory,
            system_prompt=body,
            model=model,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    async def review(
        self,
        *,
        user_prompt: str,
        yaml_text: str,
        summary_sentence: str = "",
        candidates_block: str = "",
    ) -> ReviewVerdict:
        """Ask the LLM to judge the generated workflow.

        Args:
            user_prompt: the original natural-language task.
            yaml_text: the composed workflow YAML.
            summary_sentence: optional pre-computed composition summary
                (e.g., "2 standard + 0 parameterized") to help the
                reviewer understand what was actually composed.
            candidates_block: optional listing of candidate components
                the composer saw, so the reviewer can sanity-check
                that the LLM picked from the right set.

        Returns:
            ``ReviewVerdict``. Never raises — the reviewer surfaces
            malformed output as ``approved=True, reasoning="reviewer
            response unparseable", review_used=False`` so a flaky
            reviewer can't permanently block compose.
        """
        from langchain_core.messages import HumanMessage, SystemMessage  # noqa: PLC0415

        user_msg = self._format_user_message(
            user_prompt=user_prompt,
            yaml_text=yaml_text,
            summary_sentence=summary_sentence,
            candidates_block=candidates_block,
        )
        try:
            llm = self._llm_factory(
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                model=self._model,
                base_url=self._base_url,
            )
            response = llm.invoke(
                [
                    SystemMessage(content=self._system_prompt),
                    HumanMessage(content=user_msg),
                ]
            )
            raw = getattr(response, "content", str(response))
        except Exception as exc:
            log.warning(
                "WorkflowReviewer: LLM call failed (%s: %s); "
                "falling through with approved=True so a flaky reviewer "
                "doesn't permanently block compose.",
                type(exc).__name__,
                exc,
            )
            return ReviewVerdict(
                approved=True,
                reasoning=f"reviewer unreachable: {type(exc).__name__}: {exc}",
                review_used=False,
                raw_response="",
            )

        if not isinstance(raw, str) or not raw.strip():
            return ReviewVerdict(
                approved=True,
                reasoning="reviewer returned empty content",
                review_used=False,
                raw_response="",
            )

        verdict = self._parse_verdict(raw)
        return verdict

    def _format_user_message(
        self,
        *,
        user_prompt: str,
        yaml_text: str,
        summary_sentence: str,
        candidates_block: str,
    ) -> str:
        parts: list[str] = [
            "## User task",
            "",
            user_prompt.strip(),
            "",
            "## Composed workflow YAML",
            "",
            "```yaml",
            yaml_text.strip(),
            "```",
        ]
        if summary_sentence:
            parts.extend(["", "## Composition summary", "", summary_sentence])
        if candidates_block:
            parts.extend(["", "## Candidates the composer saw", "", candidates_block])
        return "\n".join(parts)

    def _parse_verdict(self, raw: str) -> ReviewVerdict:
        """Extract the fenced JSON verdict from the LLM response.

        Robustness: tolerate the LLM's habit of emitting prose around
        the JSON. Fall through to ``approved=True, review_used=False``
        when no JSON fence parses — the reviewer should never
        permanently block a compose due to a parse hiccup. Operators
        track unparseable verdicts via the composition_summary so
        sustained drift is visible.
        """
        fence = re.search(r"```(?:json)?\s*\n(.*?)\n```", raw, flags=re.DOTALL)
        if not fence:
            log.warning(
                "WorkflowReviewer: no JSON fence in reviewer response; "
                "passing through with approved=True"
            )
            return ReviewVerdict(
                approved=True,
                reasoning="reviewer response had no JSON fence",
                review_used=False,
                raw_response=raw[:2000],
            )
        try:
            payload = json.loads(fence.group(1))
        except json.JSONDecodeError as exc:
            log.warning(
                "WorkflowReviewer: reviewer JSON did not parse (%s); "
                "passing through with approved=True",
                exc,
            )
            return ReviewVerdict(
                approved=True,
                reasoning=f"reviewer JSON parse error: {exc}",
                review_used=False,
                raw_response=raw[:2000],
            )
        if not isinstance(payload, dict):
            return ReviewVerdict(
                approved=True,
                reasoning="reviewer JSON was not an object",
                review_used=False,
                raw_response=raw[:2000],
            )
        approved = bool(payload.get("approved", True))
        reasoning = str(payload.get("reasoning", "")).strip() or "no reasoning given"
        concerns_raw = payload.get("concerns") or []
        if not isinstance(concerns_raw, list):
            concerns_raw = []
        concerns = tuple(str(c) for c in concerns_raw if isinstance(c, str))
        return ReviewVerdict(
            approved=approved,
            reasoning=reasoning,
            concerns=concerns,
            review_used=True,
            raw_response=raw[:2000],
        )


__all__ = ["ReviewVerdict", "WorkflowReviewer"]
