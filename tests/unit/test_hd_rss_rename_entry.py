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

    def test_recursive_call_sites_now_rewritten(self):
        """G105 (2026-05-17): AST rewrite now also rewrites recursive
        call sites within the entry function's body. Previously only
        the def line was rewritten and recursive references kept the
        old name (runtime NameError). After G105, ast.unparse-based
        rewrite catches self-recursion correctly."""
        code = (
            "def recursive_fn(n):\n"
            "    if n <= 1: return n\n"
            "    return recursive_fn(n - 1) + recursive_fn(n - 2)\n"
        )
        result = _rename_entry_function_if_needed(code, "fibonacci")
        # Definition renamed.
        assert "def fibonacci(n)" in result
        # CALL sites also renamed (G105 fix). The old name no longer
        # appears anywhere — runtime no longer hits NameError.
        assert "recursive_fn" not in result
        assert "fibonacci(n - 1)" in result
        assert "fibonacci(n - 2)" in result


class TestG105ASTRewriteEdgeCases:
    """G105 adds AST-based rewriting of recursive call sites. These
    edge cases pin behavior on shadowing, no-op cases, and
    deliberately-NOT-rewritten contexts."""

    def test_no_rename_when_no_recursion(self):
        """Non-recursive function: AST rewrite is a no-op on the body
        (no self-references) — only the def line changes."""
        code = "def add(a, b):\n    return a + b\n"
        result = _rename_entry_function_if_needed(code, "sum_two")
        assert "def sum_two(a, b)" in result
        # No spurious modifications to the body.
        assert "a + b" in result

    def test_helper_function_calls_to_entry_not_rewritten(self):
        """If a HELPER function (not the entry) references the entry
        function by name, the AST walker doesn't rewrite those — we
        only rewrite inside the target function's body. Limitation:
        complex orchestration patterns where helpers call back to the
        entry may need the call-site rewritten at module scope too.
        For now, document the limitation."""
        code = (
            "def helper(x):\n"
            "    return wrong_name(x) + 1\n\n"  # helper calls entry — won't be renamed
            "def wrong_name(x):\n"
            "    return x * 2\n"
        )
        result = _rename_entry_function_if_needed(code, "correct_name")
        # Entry function's def renamed.
        assert "def correct_name(x)" in result
        # Helper still references the OLD name — known limitation.
        # If this becomes a real problem, extend the AST walker to
        # rewrite Name references at module scope too.
        assert "wrong_name(x) + 1" in result
