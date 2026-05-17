"""Unit tests for the G100 token-cost accountant.

Exercises the thread-local counter, the callback wiring, and the
context-manager API. Uses fake LLM response objects so the tests
don't need a live LLM.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.benchmarks.token_accountant import (
    _CALLBACK_SINGLETON,
    count_tokens,
    install_on_llm,
)


def _llm_response_with_token_usage(prompt: int, completion: int) -> SimpleNamespace:
    """Build a fake LangChain LLMResult-shaped object carrying token
    usage at the ``llm_output["token_usage"]`` location."""
    return SimpleNamespace(
        llm_output={"token_usage": {"prompt_tokens": prompt, "completion_tokens": completion}},
        generations=[],
    )


def _llm_response_with_usage_metadata(prompt: int, completion: int) -> SimpleNamespace:
    """Build a fake response with the newer LangChain
    ``usage_metadata`` location on each generation's message."""
    msg = SimpleNamespace(usage_metadata={"input_tokens": prompt, "output_tokens": completion})
    gen = SimpleNamespace(message=msg, generation_info=None)
    return SimpleNamespace(llm_output=None, generations=[[gen]])


def _llm_response_without_tokens() -> SimpleNamespace:
    """Endpoint surfaces no token info (e.g., bare Ollama)."""
    return SimpleNamespace(llm_output=None, generations=[])


class TestCountTokensContextManager:
    def test_empty_scope_yields_zero_totals(self):
        with count_tokens() as totals:
            pass
        assert totals.prompt_tokens == 0
        assert totals.completion_tokens == 0
        assert totals.n_calls == 0

    def test_resets_on_entry(self):
        """Two sequential scopes — second sees fresh zero totals
        even if the first accumulated."""
        with count_tokens() as first:
            _CALLBACK_SINGLETON.on_llm_end(_llm_response_with_token_usage(10, 5))
        assert first.prompt_tokens == 10

        with count_tokens() as second:
            pass
        assert second.prompt_tokens == 0
        assert second.n_calls == 0

    def test_accumulates_within_scope(self):
        with count_tokens() as totals:
            _CALLBACK_SINGLETON.on_llm_end(_llm_response_with_token_usage(10, 5))
            _CALLBACK_SINGLETON.on_llm_end(_llm_response_with_token_usage(20, 8))
        assert totals.prompt_tokens == 30
        assert totals.completion_tokens == 13
        assert totals.n_calls == 2


class TestCallbackTokenExtraction:
    def test_extracts_from_llm_output_token_usage(self):
        with count_tokens() as totals:
            _CALLBACK_SINGLETON.on_llm_end(_llm_response_with_token_usage(42, 17))
        assert totals.prompt_tokens == 42
        assert totals.completion_tokens == 17
        assert totals.n_calls == 1
        assert totals.extraction_misses == 0

    def test_extracts_from_usage_metadata_on_message(self):
        """Newer LangChain location."""
        with count_tokens() as totals:
            _CALLBACK_SINGLETON.on_llm_end(_llm_response_with_usage_metadata(33, 11))
        assert totals.prompt_tokens == 33
        assert totals.completion_tokens == 11
        assert totals.n_calls == 1
        assert totals.extraction_misses == 0

    def test_records_extraction_miss_when_no_usage_data(self):
        """Ollama-no-counts case: the call happened, we know that
        (n_calls=1), but tokens=0 + extraction_misses=1 signals the
        endpoint didn't cooperate."""
        with count_tokens() as totals:
            _CALLBACK_SINGLETON.on_llm_end(_llm_response_without_tokens())
        assert totals.prompt_tokens == 0
        assert totals.completion_tokens == 0
        assert totals.n_calls == 1
        assert totals.extraction_misses == 1


class TestInstallOnLLM:
    def test_install_adds_callback_to_empty_list(self):
        llm = SimpleNamespace(callbacks=[])
        install_on_llm(llm)
        assert _CALLBACK_SINGLETON in llm.callbacks

    def test_install_is_idempotent(self):
        """Calling install_on_llm twice on the same instance should
        not double-add the callback (otherwise tokens would be
        double-counted)."""
        llm = SimpleNamespace(callbacks=[])
        install_on_llm(llm)
        install_on_llm(llm)
        assert llm.callbacks.count(_CALLBACK_SINGLETON) == 1

    def test_install_when_callbacks_is_none(self):
        """LangChain ChatOpenAI initializes callbacks=None until
        explicitly set. We handle that by setting it to a fresh list."""
        llm = SimpleNamespace(callbacks=None)
        install_on_llm(llm)
        assert llm.callbacks == [_CALLBACK_SINGLETON]


class TestOutsideScope:
    """Calls outside a count_tokens() scope should not crash. They
    increment a detached scratch totals that's discarded at the next
    scope entry."""

    def test_call_outside_scope_does_not_raise(self):
        # Ensure no scope is active.
        from tests.benchmarks import token_accountant as ta

        if hasattr(ta._local, "totals"):
            del ta._local.totals
        # This call records to the detached scratch totals.
        _CALLBACK_SINGLETON.on_llm_end(_llm_response_with_token_usage(5, 3))
        # No crash. The detached totals is implementation detail; we
        # don't expose a way to read it.

    def test_next_scope_starts_fresh_after_detached_writes(self):
        """Writes outside any scope go to a temporary; entering a
        scope sets up a fresh TokenTotals."""
        from tests.benchmarks import token_accountant as ta

        if hasattr(ta._local, "totals"):
            del ta._local.totals
        _CALLBACK_SINGLETON.on_llm_end(_llm_response_with_token_usage(100, 50))
        with count_tokens() as totals:
            assert totals.prompt_tokens == 0
            assert totals.n_calls == 0
            _CALLBACK_SINGLETON.on_llm_end(_llm_response_with_token_usage(7, 3))
        assert totals.prompt_tokens == 7


class TestUnknownCallbackHook:
    """Defensive __getattr__ — LangChain's BaseCallbackHandler API
    surface varies by version. Unknown callback hooks should no-op."""

    def test_unknown_on_callback_returns_noop(self):
        noop = _CALLBACK_SINGLETON.on_some_unknown_hook
        assert callable(noop)
        # Should not raise even with arbitrary args.
        assert noop("x", "y", foo="bar") is None

    def test_non_callback_attribute_raises(self):
        """Anything that doesn't start with ``on_`` is a real
        AttributeError — we don't pretend to have arbitrary attrs."""
        with pytest.raises(AttributeError):
            _ = _CALLBACK_SINGLETON.some_random_attribute
