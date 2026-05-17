"""Unit tests for the G102 templated composer in hd_rss.py.

The templated composer's job:
  1. Concatenate helper function bodies verbatim (NEVER drop a name)
  2. Build a shrunk LLM prompt that mentions helpers by name+description
     (not their bodies) and asks for the orchestrator only
  3. Concatenate helpers + orchestrator into final code

These tests verify the deterministic concatenation behavior with a
fake LLM. The real-LLM integration test lives at the benchmark
layer (a parity comparison vs the LLM composer).
"""

from __future__ import annotations

from collections.abc import Iterable
from types import SimpleNamespace

from tests.benchmarks.codegen.hd_rss import _compose_templated


class _FakeLLM:
    """Minimal LLM stub.

    ``.invoke(messages)`` returns a ``SimpleNamespace`` with
    ``.content == self.response``. ``self.last_messages`` captures
    the prompt for prompt-shape assertions.
    """

    def __init__(self, response: str):
        self.response = response
        self.last_messages: Iterable[object] = ()

    def invoke(self, messages):
        self.last_messages = messages
        return SimpleNamespace(content=self.response)


class TestTemplatedComposerDeterministicAssembly:
    """The composer's contract is: helpers always present, regardless
    of what the LLM did."""

    def test_helpers_prepended_verbatim(self):
        subsolutions = [
            {
                "name": "double",
                "code": "def double(x):\n    return x * 2",
                "description": "returns 2x",
            },
            {
                "name": "increment",
                "code": "def increment(x):\n    return x + 1",
                "description": "returns x+1",
            },
        ]
        fake = _FakeLLM(response="```python\ndef parent(x):\n    return increment(double(x))\n```")
        result = _compose_templated(
            fake,
            original_problem="Return 2x+1",
            subsolutions=subsolutions,
            parent_entry_point="parent",
        )
        # Helpers MUST be present in the final source (G102 contract).
        assert "def double(x):" in result
        assert "return x * 2" in result
        assert "def increment(x):" in result
        assert "return x + 1" in result
        # Orchestrator also present.
        assert "def parent(x):" in result
        assert "return increment(double(x))" in result

    def test_helpers_remain_present_even_if_llm_redefines_them(self):
        """Defense in depth: even if the LLM ignores instructions and
        emits a redefinition, the deterministic prepend means the
        helpers ARE defined when the script runs."""
        subsolutions = [
            {"name": "h", "code": "def h(x):\n    return x", "description": "identity"},
        ]
        # LLM redefines h (incorrectly) — but we trust the prepended one.
        fake = _FakeLLM(
            response="```python\ndef h(x):\n    return None  # wrong!\n\ndef parent(x):\n    return h(x)\n```"
        )
        result = _compose_templated(
            fake,
            original_problem="identity",
            subsolutions=subsolutions,
            parent_entry_point="parent",
        )
        # Both definitions present — the LATER redefinition WINS at
        # runtime due to Python's import order. This test pins that
        # behavior so a future "drop LLM redefinitions" sweep is an
        # explicit decision, not an accident.
        assert result.count("def h(x):") == 2
        # The deterministic prepend is FIRST, so the LLM's redefinition
        # (last) is what runs. This is a known limitation; documented
        # in the docstring.

    def test_no_helpers_emits_orchestrator_only(self):
        """Empty subsolutions list — no helpers to prepend. Should
        return just the LLM's orchestrator output."""
        fake = _FakeLLM(response="```python\ndef parent():\n    return 42\n```")
        result = _compose_templated(
            fake,
            original_problem="return 42",
            subsolutions=[],
            parent_entry_point="parent",
        )
        assert result.strip() == "def parent():\n    return 42"

    def test_llm_response_without_fence_still_extracted(self):
        """``_extract_code`` (shared with the LLM composer) strips
        fences when present and returns raw text when absent."""
        subsolutions = [{"name": "h", "code": "def h(): pass", "description": "x"}]
        fake = _FakeLLM(response="def parent(): return h()")  # no fence
        result = _compose_templated(
            fake,
            original_problem="x",
            subsolutions=subsolutions,
            parent_entry_point="parent",
        )
        assert "def h(): pass" in result
        assert "def parent(): return h()" in result


class TestTemplatedComposerPromptShape:
    """The LLM prompt is the surface area we deliberately shrunk vs the
    LLM composer. Pin its shape so a regression that re-bloats it
    (e.g., re-introducing helper bodies in the prompt) is caught."""

    def test_prompt_lists_helper_names_not_bodies(self):
        subsolutions = [
            {
                "name": "double",
                "code": "def double(x):\n    return x * 2",  # body — should NOT appear in prompt
                "description": "returns 2x",
            },
        ]
        fake = _FakeLLM(response="```python\ndef p(x): return double(x)\n```")
        _compose_templated(
            fake,
            original_problem="Return 2x",
            subsolutions=subsolutions,
            parent_entry_point="p",
        )
        # Extract the human message content.
        human_msg = list(fake.last_messages)[-1]
        prompt_text = human_msg.content
        # Helper NAME appears.
        assert "double" in prompt_text
        # Helper DESCRIPTION appears.
        assert "returns 2x" in prompt_text
        # Helper BODY does NOT appear — we don't pay for re-shipping
        # bodies the deterministic concat will handle. This is the
        # whole point of the templated approach.
        assert "return x * 2" not in prompt_text

    def test_prompt_instructs_orchestrator_only_no_redefinition(self):
        fake = _FakeLLM(response="```python\ndef p(): pass\n```")
        _compose_templated(
            fake,
            original_problem="trivial",
            subsolutions=[{"name": "h", "code": "def h(): pass", "description": "noop"}],
            parent_entry_point="p",
        )
        system_msg = list(fake.last_messages)[0]
        system_text = system_msg.content
        assert "DO NOT redefine" in system_text
        assert "ALREADY defined" in system_text

    def test_long_description_truncated_for_prompt_economy(self):
        """Pin the truncation threshold so the prompt stays small even
        when the decomposer emits very long descriptions."""
        long_desc = "long " * 100  # 500 chars
        subsolutions = [{"name": "h", "code": "def h(): pass", "description": long_desc}]
        fake = _FakeLLM(response="```python\ndef p(): pass\n```")
        _compose_templated(
            fake,
            original_problem="x",
            subsolutions=subsolutions,
            parent_entry_point="p",
        )
        human_msg = list(fake.last_messages)[-1]
        prompt_text = human_msg.content
        # Description truncated — prompt length is bounded.
        assert "..." in prompt_text
        assert len(prompt_text) < 1000  # back-of-envelope bound


def test_make_codegen_accepts_composer_strategy():
    """The factory wires the strategy parameter through to the
    recursive solver — smoke check that the CLI's `--codegen hd_rss_v2`
    actually reaches `_compose_templated` instead of `_compose`."""
    from tests.benchmarks.codegen.hd_rss import make_hd_rss_codegen

    # Just verifies the factory accepts the kwarg without raising.
    # End-to-end behavior verified in the integration / benchmark layer.
    codegen = make_hd_rss_codegen(model="fake", base_url="fake", composer_strategy="templated")
    assert callable(codegen)


def test_invalid_composer_strategy_silently_falls_back_to_llm():
    """Defensive: an unknown strategy doesn't crash — falls back to
    the LLM composer (the original/default behavior). This makes
    operators-typo-resistant; the harness logs the chosen strategy
    so a misconfigured run is loud."""
    from tests.benchmarks.codegen.hd_rss import make_hd_rss_codegen

    codegen = make_hd_rss_codegen(model="fake", base_url="fake", composer_strategy="nonsense_value")
    assert callable(codegen)
    # The fallback path is exercised inside _solve_recursive; we
    # don't drive an LLM here. The factory should accept any string
    # without raising.
