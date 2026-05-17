"""Unit tests for the G103 best-of-N direct codegen.

Mocks the LLM + the sandbox to exercise the early-return logic
without driving real subprocesses. Empirical integration coverage
is the benchmark layer (matching the workspace's unit-mock /
integration-test parity rule — the benchmark IS the integration
test against real Ollama + real subprocess sandbox).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from tests.benchmarks.codegen.best_of_n import make_best_of_n_codegen
from tests.benchmarks.types import BenchmarkProblem


def _problem(pid: str = "x/1") -> BenchmarkProblem:
    return BenchmarkProblem(
        problem_id=pid,
        prompt="Write add(a, b) returning a+b.",
        setup_code="",
        test_code="assert add(2, 3) == 5\n",
        entry_point="add",
    )


def _make_llm_returning(*responses: str):
    """Build a fake LLM whose ``.invoke()`` returns the given
    response strings in order. Captures invocation count."""
    iterator = iter(responses)
    invocations = []

    class _FakeLLM:
        def invoke(self, messages):
            invocations.append(messages)
            try:
                return SimpleNamespace(content=next(iterator))
            except StopIteration as e:
                # Defensive — test should not invoke more than expected.
                raise AssertionError(
                    "Best-of-N invoked LLM more times than test prepared for"
                ) from e

    return _FakeLLM(), invocations


class TestEarlyReturnOnFirstPass:
    """First sample passes → return immediately, no further LLM/exec
    calls. This is the headline cost-savings of best-of-N: in the
    common case where the LLM gets it right, you pay 1× cost."""

    def test_first_sample_passes_returns_after_one_llm_call(self):
        llm, invocations = _make_llm_returning(
            "```python\ndef add(a, b): return a + b\n```",
        )
        codegen = make_best_of_n_codegen(
            model="fake",
            base_url="fake",
            n_samples=3,
            llm_factory=lambda **_: llm,
        )

        # Mock the sandbox to claim the first sample passes.
        with mock.patch(
            "tests.benchmarks.codegen.best_of_n.run_in_subprocess",
            return_value=SimpleNamespace(
                passed=True, timed_out=False, stdout="", stderr="", exit_code=0
            ),
        ):
            result = codegen(_problem())

        assert "def add(a, b): return a + b" in result
        # Only ONE LLM call despite n_samples=3.
        assert len(invocations) == 1


class TestRetrySamplesUntilPass:
    """First fails, second passes → return second, no third call."""

    def test_second_sample_passes_returns_after_two_llm_calls(self):
        llm, invocations = _make_llm_returning(
            "```python\ndef add(a, b): return None  # wrong\n```",
            "```python\ndef add(a, b): return a + b  # right\n```",
        )
        codegen = make_best_of_n_codegen(
            model="fake",
            base_url="fake",
            n_samples=3,
            llm_factory=lambda **_: llm,
        )

        # First sandbox call fails, second passes.
        sandbox_results = [
            SimpleNamespace(
                passed=False, timed_out=False, stdout="", stderr="AssertionError", exit_code=1
            ),
            SimpleNamespace(passed=True, timed_out=False, stdout="", stderr="", exit_code=0),
        ]
        with mock.patch(
            "tests.benchmarks.codegen.best_of_n.run_in_subprocess",
            side_effect=sandbox_results,
        ):
            result = codegen(_problem())

        # The PASSING sample is returned, not the failing one.
        assert "right" in result
        assert "wrong" not in result
        # Exactly 2 LLM calls — no wasted third sample.
        assert len(invocations) == 2


class TestAllFailReturnsLast:
    """All N samples fail → return the LAST sample so the runner
    records a real failure with real error_class. We deliberately do
    NOT synthesize an "empty" return — the runner needs a real
    failed-code-source to put in the result JSON."""

    def test_all_n_fail_returns_last_sample(self):
        responses = [
            "```python\ndef add(a, b): return 0\n```",
            "```python\ndef add(a, b): return 1\n```",
            "```python\ndef add(a, b): return 2\n```",
        ]
        llm, invocations = _make_llm_returning(*responses)
        codegen = make_best_of_n_codegen(
            model="fake",
            base_url="fake",
            n_samples=3,
            llm_factory=lambda **_: llm,
        )

        # All sandbox calls fail.
        sandbox_result = SimpleNamespace(
            passed=False, timed_out=False, stdout="", stderr="AssertionError", exit_code=1
        )
        with mock.patch(
            "tests.benchmarks.codegen.best_of_n.run_in_subprocess",
            return_value=sandbox_result,
        ):
            result = codegen(_problem())

        # The LAST sample (n=3 → return 2) is what we get back.
        assert "return 2" in result
        # All 3 samples were attempted.
        assert len(invocations) == 3


class TestSandboxExceptionTreatedAsFailure:
    """If the sandbox itself crashes (e.g., subprocess startup
    failure), treat that sample as failed and try the next. This
    keeps the loop alive in the face of infrastructure flakes."""

    def test_sandbox_exception_advances_to_next_sample(self):
        llm, invocations = _make_llm_returning(
            "```python\ndef add(a, b): return 0\n```",  # sandbox crash on this
            "```python\ndef add(a, b): return a + b\n```",  # sandbox passes
        )
        codegen = make_best_of_n_codegen(
            model="fake",
            base_url="fake",
            n_samples=3,
            llm_factory=lambda **_: llm,
        )

        call_count = [0]

        def _sandbox(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("simulated sandbox crash")
            return SimpleNamespace(passed=True, timed_out=False, stdout="", stderr="", exit_code=0)

        with mock.patch(
            "tests.benchmarks.codegen.best_of_n.run_in_subprocess",
            side_effect=_sandbox,
        ):
            result = codegen(_problem())

        # Got the second sample (after the crash on the first).
        assert "return a + b" in result
        assert len(invocations) == 2


class TestEmptyCodeReturnsFalseWithoutSandboxCall:
    """If the LLM returns empty, skip the sandbox call (no point) and
    move to the next sample."""

    def test_empty_response_skips_sandbox(self):
        llm, invocations = _make_llm_returning(
            "",  # empty
            "```python\ndef add(a, b): return a + b\n```",
        )
        codegen = make_best_of_n_codegen(
            model="fake",
            base_url="fake",
            n_samples=3,
            llm_factory=lambda **_: llm,
        )

        sandbox_calls = []

        def _sandbox(**kwargs):
            sandbox_calls.append(kwargs)
            return SimpleNamespace(passed=True, timed_out=False, stdout="", stderr="", exit_code=0)

        with mock.patch(
            "tests.benchmarks.codegen.best_of_n.run_in_subprocess",
            side_effect=_sandbox,
        ):
            result = codegen(_problem())

        # Second sample returned.
        assert "return a + b" in result
        # Sandbox was called only once (for the second sample) — the
        # empty first sample was short-circuited.
        assert len(sandbox_calls) == 1


def test_n_samples_default_is_three():
    """Pin the default — operators expect this knob to stay reasonable."""
    import inspect

    from tests.benchmarks.codegen.best_of_n import make_best_of_n_codegen

    sig = inspect.signature(make_best_of_n_codegen)
    assert sig.parameters["n_samples"].default == 3


def test_default_temperature_is_nonzero():
    """The whole point of best-of-N is sampling diversity. A default
    of 0 (== deterministic == direct) would defeat the purpose. Pin
    it above zero."""
    import inspect

    from tests.benchmarks.codegen.best_of_n import make_best_of_n_codegen

    sig = inspect.signature(make_best_of_n_codegen)
    assert sig.parameters["temperature"].default > 0.0
