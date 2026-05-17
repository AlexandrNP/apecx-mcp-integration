"""Unit tests for the G102b entry-point rename in hd_rss.

The rename pass closes the actual root cause of HD-RSS's 17 MBPP
NameErrors: the LLM normalizes function names to lowercase
snake_case regardless of the requested entry_point. G101's analysis
mis-attributed this to the composer; the real bug was in
_atomic_codegen + composers ignoring the entry_point hint.

These tests pin the rename behavior in isolation. The empirical
validation lives in the benchmark layer (hd_rss_v2 N=100 re-run).
"""

from __future__ import annotations

from tests.benchmarks.codegen.hd_rss import _rename_entry_function_if_needed


class TestRenameApplied:
    def test_single_function_rename_when_mismatch(self):
        code = "def find_max_num(digits):\n    return max(digits)\n"
        result = _rename_entry_function_if_needed(code, "find_Max_Num")
        assert "def find_Max_Num(digits):" in result
        # Original definition removed (only 1 def in the result).
        assert "def find_max_num(" not in result

    def test_multiple_functions_rename_last_only(self):
        """Helper functions defined first; orchestrator (entry) is
        conventionally last. Rename only the last."""
        code = (
            "def helper(x):\n    return x * 2\n\n"
            "def wrong_main_name(x):\n    return helper(x) + 1\n"
        )
        result = _rename_entry_function_if_needed(code, "correct_name")
        assert "def helper(x):" in result  # helper unchanged
        assert "def correct_name(x):" in result
        assert "def wrong_main_name(" not in result

    def test_no_rename_when_name_already_matches(self):
        code = "def add(a, b):\n    return a + b\n"
        result = _rename_entry_function_if_needed(code, "add")
        assert result == code  # exact same bytes


class TestRenameSkipped:
    """When rename is impossible or unnecessary, return the input
    unchanged. We never raise — the caller's downstream sandbox will
    surface the real error class if the code is malformed."""

    def test_none_requested_name_no_op(self):
        code = "def foo(): pass\n"
        assert _rename_entry_function_if_needed(code, None) == code

    def test_empty_requested_name_no_op(self):
        code = "def foo(): pass\n"
        assert _rename_entry_function_if_needed(code, "") == code

    def test_empty_code_no_op(self):
        assert _rename_entry_function_if_needed("", "anything") == ""

    def test_unparseable_code_returned_as_is(self):
        bad = "def broken(:\n    syntax error\n"
        assert _rename_entry_function_if_needed(bad, "fixed") == bad

    def test_no_functions_in_code_no_op(self):
        code = "x = 42\nprint(x)\n"
        assert _rename_entry_function_if_needed(code, "any_name") == code


class TestEdgeCases:
    def test_function_with_type_annotations(self):
        code = "def long_name(a: int, b: int) -> int:\n    return a + b\n"
        result = _rename_entry_function_if_needed(code, "short")
        assert "def short(a: int, b: int) -> int:" in result
        assert "def long_name(" not in result

    def test_function_with_decorator_renames(self):
        """The pattern matches the ``def`` line itself, not the
        decorator. Decorator stays attached."""
        code = "@staticmethod\ndef foo(x):\n    return x\n"
        result = _rename_entry_function_if_needed(code, "bar")
        assert "@staticmethod" in result
        assert "def bar(x):" in result

    def test_call_sites_within_code_not_renamed(self):
        """We rename the DEFINITION only. Call sites inside the code
        (e.g., recursive calls) would break if renamed in mismatch.
        Document this trade-off — recursive functions whose name is
        renamed will fail at runtime. For the MBPP / SciCode classes
        where this rename helps, recursion is rare."""
        code = (
            "def recursive_fn(n):\n"
            "    if n <= 1: return n\n"
            "    return recursive_fn(n - 1) + recursive_fn(n - 2)\n"
        )
        result = _rename_entry_function_if_needed(code, "fibonacci")
        # Definition renamed.
        assert "def fibonacci(n):" in result
        # CALL sites preserved (still refer to old name) — caller
        # gets a NameError at runtime, which is a worse failure than
        # the original. This test documents the known limitation;
        # a future enhancement could AST-rewrite call sites too.
        assert "recursive_fn(n - 1)" in result
