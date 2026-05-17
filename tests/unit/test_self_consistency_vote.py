"""Unit tests for G108 self-consistency vote.

The interesting testable surface is the AST signature + voting
logic. End-to-end coverage against real Ollama is the benchmark
layer (per workspace's unit-mock / integration-test parity rule).
"""

from __future__ import annotations

from tests.benchmarks.codegen.self_consistency_vote import (
    _normalized_ast_signature,
    _vote,
)


class TestNormalizedASTSignature:
    def test_function_def_captures_name_and_body_shape(self):
        sig = _normalized_ast_signature("def f(x):\n    return x * 2\n")
        # Function name + a single-element body-types tuple.
        assert "def:f" in sig
        assert "Return" in sig

    def test_two_equivalent_functions_have_same_signature(self):
        a = "def f(x):\n    return x + 1\n"
        b = "def f(y):\n    return y + 1\n"  # different arg name, same shape
        assert _normalized_ast_signature(a) == _normalized_ast_signature(b)

    def test_different_function_names_yield_different_signatures(self):
        a = "def f(x): return x"
        b = "def g(x): return x"
        assert _normalized_ast_signature(a) != _normalized_ast_signature(b)

    def test_different_body_shape_yields_different_signatures(self):
        a = "def f(x):\n    return x\n"  # single Return
        b = "def f(x):\n    y = x + 1\n    return y\n"  # Assign + Return
        assert _normalized_ast_signature(a) != _normalized_ast_signature(b)

    def test_unparseable_code_returns_none(self):
        assert _normalized_ast_signature("def broken(:\n    syntax err\n") is None

    def test_empty_code_returns_none(self):
        assert _normalized_ast_signature("") is None
        assert _normalized_ast_signature("   \n") is None

    def test_module_level_statements_captured(self):
        sig = _normalized_ast_signature("x = 1\ny = 2\nprint(x + y)\n")
        # Three top-level statements.
        assert "Assign" in sig
        assert "Expr" in sig


class TestVote:
    def test_unanimous_vote_picks_any_match(self):
        samples = [
            "def f(x): return x",
            "def f(y): return y",
            "def f(z): return z",
        ]
        winner = _vote(samples)
        # All have same signature; the first wins by tiebreaker.
        assert winner == 0

    def test_majority_vote_picks_majority(self):
        samples = [
            "def f(x): return x",  # majority (2 of 3)
            "def f(x):\n    y = x\n    return y\n",  # outlier
            "def f(x): return x",  # majority
        ]
        winner = _vote(samples)
        # Majority signature; first occurrence of it wins.
        assert winner == 0

    def test_no_majority_three_way_tie_picks_first(self):
        samples = [
            "def f(x): return x",
            "def f(x):\n    y = x\n    return y\n",
            "def f(x):\n    return x + 1\n",
        ]
        # All three different. Counter.most_common(1) returns one of
        # them — order is insertion order in Python 3.7+. The first
        # signature in iteration is the first sample's, so it wins.
        winner = _vote(samples)
        assert winner == 0

    def test_unparseable_samples_excluded_from_voting(self):
        samples = [
            "def broken(:",  # unparseable
            "def f(x): return x",  # only valid one
            "def broken_also",  # unparseable
        ]
        winner = _vote(samples)
        # Sample 1 is the only valid one → it wins.
        assert winner == 1

    def test_all_unparseable_falls_back_to_index_zero(self):
        samples = ["broken 1", "broken 2", "broken 3"]
        winner = _vote(samples)
        # No vote possible → fall back to index 0; runner will
        # surface SyntaxError on the final exec.
        assert winner == 0

    def test_n_equals_two_picks_first_on_disagreement(self):
        samples = [
            "def f(x): return x",
            "def f(x):\n    y = x\n    return y\n",
        ]
        winner = _vote(samples)
        # Counter ties; first key in insertion order wins (sample 0).
        assert winner == 0

    def test_minority_outlier_loses(self):
        """3 majority + 1 outlier — majority wins."""
        samples = [
            "def f(x): return x",  # majority
            "def f(x): return x * 2",  # outlier (different body)
            "def f(x): return x",
            "def f(x): return x",
        ]
        winner = _vote(samples)
        assert winner == 0  # first occurrence of the majority signature
