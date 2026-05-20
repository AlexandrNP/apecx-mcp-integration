"""Unit tests for LLMTaskDecomposer (EO-20 decomposer impl) — stub LLM, deterministic."""

from __future__ import annotations

import asyncio

import pytest

from apecx_integration.composition.decomposition.llm_decomposer import LLMTaskDecomposer
from apecx_integration.composition.decomposition.local_decomposer import Task


class _StubLLM:
    def __init__(self, content: str):
        self._content = content

    def invoke(self, _messages):
        class _R:
            content = self._content

        return _R()


def _decomposer(content: str, **kw) -> LLMTaskDecomposer:
    return LLMTaskDecomposer(llm_factory=lambda **_: _StubLLM(content), **kw)


def test_decomposable_returns_subtasks():
    d = _decomposer('{"decomposable": true, "subtasks": ["find epitopes", "rank by evidence"]}')
    subs = asyncio.run(d.decompose(Task("epitope analysis")))
    assert [t.description for t in subs] == ["find epitopes", "rank by evidence"]


def test_not_decomposable_returns_empty():
    d = _decomposer('{"decomposable": false, "subtasks": []}')
    assert asyncio.run(d.decompose(Task("atomic task"))) == []


def test_strips_json_fence():
    d = _decomposer('```json\n{"decomposable": true, "subtasks": ["a"]}\n```')
    subs = asyncio.run(d.decompose(Task("x")))
    assert [t.description for t in subs] == ["a"]


def test_salvages_json_from_prose():
    d = _decomposer(
        'Sure! Here is the plan:\n{"decomposable": true, "subtasks": ["a", "b"]}\nHope that helps.'
    )
    subs = asyncio.run(d.decompose(Task("x")))
    assert [t.description for t in subs] == ["a", "b"]


def test_garbage_raises_loudly():
    d = _decomposer("this is not json at all")
    with pytest.raises(ValueError):
        asyncio.run(d.decompose(Task("x")))


def test_missing_decomposable_key_raises():
    d = _decomposer('{"subtasks": ["a"]}')
    with pytest.raises(ValueError):
        asyncio.run(d.decompose(Task("x")))


def test_empty_response_raises():
    d = _decomposer("   ")
    with pytest.raises(ValueError):
        asyncio.run(d.decompose(Task("x")))


def test_max_subtasks_cap():
    subs_json = '{"decomposable": true, "subtasks": ["a","b","c","d","e","f","g","h"]}'
    d = _decomposer(subs_json, max_subtasks=3)
    subs = asyncio.run(d.decompose(Task("x")))
    assert len(subs) == 3


def test_subtasks_not_a_list_raises():
    d = _decomposer('{"decomposable": true, "subtasks": "not-a-list"}')
    with pytest.raises(ValueError):
        asyncio.run(d.decompose(Task("x")))


def test_blank_subtask_entries_dropped():
    d = _decomposer('{"decomposable": true, "subtasks": ["a", "   ", "b"]}')
    subs = asyncio.run(d.decompose(Task("x")))
    assert [t.description for t in subs] == ["a", "b"]
