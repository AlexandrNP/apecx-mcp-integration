"""LLM-driven task decomposer (EO-20 decomposer impl) — the local-LLM boundary.

``LLMTaskDecomposer`` is the only non-deterministic part of the decomposition path: it asks a
local LLM whether a task is decomposable and, if so, for its sub-tasks. The prompt lives in a
file (``prompts/decompose.md``) — an LLM-guiding artifact kept separate from code (no hardcoded
prompts), imperative + schema only (rationale lives in docs, not the prompt).

Loud by design: an empty LLM response or an unparseable one raises ``ValueError`` (a parse
failure is NOT silently treated as "not decomposable" — that would mask a broken model behind a
plausible-looking "cannot solve").
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from apecx_integration.agents._llm_factory import build_chat_llm
from apecx_integration.composition.decomposition.local_decomposer import Task

_DEFAULT_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "decompose.md"
_FENCE_OPEN = re.compile(r"^```(?:json)?\s*", re.IGNORECASE)
_FENCE_CLOSE = re.compile(r"\s*```$")
_FIRST_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def _parse_decomposition(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = _FENCE_CLOSE.sub("", _FENCE_OPEN.sub("", text)).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Local models sometimes wrap the JSON in prose; salvage the first {...} block.
        m = _FIRST_OBJECT.search(text)
        if m is None:
            raise ValueError(
                f"LLMTaskDecomposer: response is not valid JSON and contains no JSON object: "
                f"{content[:200]!r}"
            ) from None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise ValueError(
                f"LLMTaskDecomposer: response is not valid JSON: {e}; got {content[:200]!r}"
            ) from e
    if not isinstance(data, dict) or "decomposable" not in data:
        raise ValueError(
            f"LLMTaskDecomposer: response missing required 'decomposable' key: {data!r}"
        )
    return data


class LLMTaskDecomposer:
    def __init__(
        self,
        *,
        prompt_path: str | Path | None = None,
        llm_factory: Callable[..., Any] = build_chat_llm,
        max_subtasks: int = 5,
    ) -> None:
        self._prompt = Path(prompt_path or _DEFAULT_PROMPT_PATH).read_text(encoding="utf-8")
        self._llm_factory = llm_factory
        self._max_subtasks = max_subtasks

    async def decompose(self, task: Task) -> list[Task]:
        llm = self._llm_factory(temperature=0.0)
        messages = [
            {"role": "system", "content": self._prompt},
            {"role": "user", "content": task.description},
        ]
        # ChatOpenAI.invoke is sync/blocking; offload so we don't block the event loop.
        resp = await asyncio.to_thread(llm.invoke, messages)
        content = getattr(resp, "content", None)
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"LLMTaskDecomposer: empty LLM response for task {task.description!r}")
        data = _parse_decomposition(content)
        if not data.get("decomposable"):
            return []
        subtasks = data.get("subtasks") or []
        if not isinstance(subtasks, list):
            raise ValueError(
                f"LLMTaskDecomposer: 'subtasks' must be a list, got {type(subtasks).__name__}"
            )
        return [Task(str(s)) for s in subtasks[: self._max_subtasks] if str(s).strip()]
