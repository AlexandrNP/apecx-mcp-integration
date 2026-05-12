"""Supervised-authoring integration tests for the two new workflows.

The premise: **Ollama AUTHORS the broken/bare code, then Ollama
DEBUGS or DOCUMENTS its own output via the new workflows.** The
human supervisor (this test file) provides the framework
scaffolding + correctness gates, not the substantive code.

Two tests:

  1. ``test_bug_fix_workflow_on_ollama_authored_broken_code`` —
     Ollama writes a deliberately broken ``fizzbuzz`` (via
     CodeWriteStep with a "introduce one specific off-by-one"
     spec). The iterative_bug_fix_workflow then drives Ollama to
     fix its own bug. Verification: ``exec_succeeded == True`` on
     the test assertion AFTER the fix.

  2. ``test_documentation_workflow_on_ollama_authored_bare_code`` —
     Ollama writes bare ``add(a, b)`` code (no docstring) via
     CodeWriteStep, then the code_documentation_workflow drives
     Ollama to add a docstring. Verification: output has a
     docstring (ast.get_docstring(funcdef) is non-empty) AND the
     function body's AST (modulo the docstring expression) is
     unchanged.

Honest pass criteria:
  - Bug-fix: ONE iteration of the workflow. v1 ships a single-pass
    fix. If the model's first fix doesn't pass, we record the
    failure honestly via xfail-with-strict=False (some 12B model
    runs miss the fix on the first try; that's a measurement, not
    a regression).
  - Documentation: AST-body equivalence is the strict gate; docs
    presence is the soft gate.

Auto-skips when Ollama is unreachable.
"""

from __future__ import annotations

import ast
import asyncio
import os
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
CODE_WRITING_DIR = (
    REPO_ROOT / "src" / "apecx_integration" / "composition" / "workflows" / "code_writing"
)


def _llm_reachable() -> bool:
    base = os.environ.get("APECX_LLM_BASE_URL") or "http://localhost:11434/v1"
    probe = base[:-3] + "/api/tags" if base.endswith("/v1") else base.rstrip("/") + "/api/tags"
    try:
        return httpx.get(probe, timeout=2.0).status_code == 200
    except Exception:
        return False


SKIP_LLM = "LLM not reachable — set APECX_LLM_BASE_URL"
SKIP_EXEC = "Set APECX_CODE_EXEC=1 to run bug-fix workflow (uses subprocess)"


def _ast_body_signature(code: str, function_name: str) -> tuple[str, ...]:
    """Return a tuple of AST dumps of the function body's statements,
    EXCLUDING the leading docstring (an ast.Expr with ast.Constant
    string). Two functions have the same signature iff their bodies
    are AST-equivalent (modulo docstring + whitespace)."""
    tree = ast.parse(code)
    target = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            target = node
            break
    if target is None:
        return ()
    body = target.body
    # Drop leading docstring.
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return tuple(ast.dump(stmt) for stmt in body)


def _has_docstring(code: str, function_name: str) -> str | None:
    tree = ast.parse(code)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            return ast.get_docstring(node)
    return None


@pytest.mark.skipif(not _llm_reachable(), reason=SKIP_LLM)
@pytest.mark.skipif(os.environ.get("APECX_CODE_EXEC") != "1", reason=SKIP_EXEC)
def test_bug_fix_workflow_on_ollama_authored_broken_code(tmp_path):
    """Two-stage test:
    Stage 1: Ollama AUTHORS a deliberately broken fizzbuzz.
    Stage 2: iterative_bug_fix_workflow drives Ollama to FIX its
             own bug; we exec-verify the fix.
    """
    from apecx_integration.composition.steps.code_write_step import (
        CodeWriteStep,
    )
    from apecx_integration.composition.steps.isolated_py_exec_step import (
        IsolatedPyExecStep,
    )

    # STAGE 1: ask Ollama to author broken fizzbuzz on purpose.
    code_write = CodeWriteStep.from_config(str(CODE_WRITING_DIR / "steps" / "code_write.yml"))
    bug_spec = (
        "Write a function fizzbuzz(n: int) -> str. Multiples of 3 "
        "return 'Fizz'; multiples of 5 return 'Buzz'; multiples of "
        "BOTH return 'FizzBuzz'; otherwise return str(n). "
        "IMPORTANT: as an EXERCISE for a downstream bug-fix tool, "
        "introduce exactly ONE off-by-one or wrong-modulo bug in "
        "your implementation (do NOT make it correct). For "
        "example: check `n % 4 == 0` instead of `n % 3 == 0`, OR "
        "return 'Buzz' for n % 3 instead of 'Fizz'. Keep everything "
        "else right."
    )

    start = time.monotonic()
    broken = asyncio.run(
        code_write.process(
            {
                "code_spec": bug_spec,
                "function_name": "fizzbuzz",
                "function_signature": "def fizzbuzz(n: int) -> str",
            }
        )
    )
    write_elapsed = time.monotonic() - start
    broken_code = broken["code_source"]
    print(
        f"\n[bug-fix stage 1] elapsed={write_elapsed:.2f}s; "
        f"broken_code preview={broken_code[:120]!r}"
    )

    # Confirm Ollama actually produced parseable Python with the
    # right function name (writer gates already enforced this).
    assert broken["function_name_verified"] == "fizzbuzz"

    # The test_code we'll run against the patched version.
    test_assertion = (
        "assert fizzbuzz(3) == 'Fizz', f'fizzbuzz(3) returned {fizzbuzz(3)!r}'\n"
        "assert fizzbuzz(5) == 'Buzz'\n"
        "assert fizzbuzz(15) == 'FizzBuzz'\n"
        "assert fizzbuzz(7) == '7'\n"
    )

    # First, run the broken code through IsolatedPyExecStep to
    # capture the actual error trace. This is what becomes the
    # "critique" input to the bug-fix step.
    exec_step = IsolatedPyExecStep.from_config(
        str(CODE_WRITING_DIR / "steps" / "isolated_py_exec.yml")
    )
    broken_run = asyncio.run(
        exec_step.process({"code_source": broken_code, "test_code": test_assertion})
    )
    print(
        f"[bug-fix stage 1b] broken exec_succeeded="
        f"{broken_run['exec_succeeded']}; "
        f"stderr_preview={broken_run['stderr'][:160]!r}"
    )
    # The bug should actually break — if Ollama wrote correct code
    # despite our instruction, this test loses its point. Honest
    # measurement: log but don't fail.
    if broken_run["exec_succeeded"]:
        pytest.skip(
            "Ollama ignored the 'introduce a bug' instruction and "
            "wrote correct code; bug-fix exercise is vacuous. This "
            "is observed behavior (some prompts on mistral-nemo "
            "produce correct fizzbuzz no matter what); not a "
            "regression."
        )

    error_trace = broken_run["stderr"]

    # STAGE 2: drive the bug_fix_write step + verifier.
    bug_fix_write = CodeWriteStep.from_config(str(CODE_WRITING_DIR / "steps" / "bug_fix_write.yml"))
    start_fix = time.monotonic()
    fixed = asyncio.run(
        bug_fix_write.process(
            {
                "code_spec": bug_spec.replace(
                    "introduce exactly ONE off-by-one or wrong-modulo bug",
                    "MAKE IT CORRECT",
                ),
                "function_name": "fizzbuzz",
                "function_signature": "def fizzbuzz(n: int) -> str",
                "previous_attempt": broken_code,
                "critique": error_trace,
            }
        )
    )
    fix_elapsed = time.monotonic() - start_fix
    fixed_code = fixed["code_source"]
    print(
        f"\n[bug-fix stage 2] elapsed={fix_elapsed:.2f}s; fixed_code preview={fixed_code[:120]!r}"
    )

    # Verify the fixed code passes the test.
    fixed_run = asyncio.run(
        exec_step.process({"code_source": fixed_code, "test_code": test_assertion})
    )
    print(
        f"[bug-fix stage 2b] fixed exec_succeeded="
        f"{fixed_run['exec_succeeded']}; "
        f"stderr_preview={fixed_run['stderr'][:160]!r}"
    )

    # Pin: the fixed code MUST pass the test assertion. If it
    # doesn't, the bug-fix workflow failed on this attempt — a
    # measurement, not a hard regression. We assert success because
    # the prompt + gate design SHOULD work for a trivial fizzbuzz
    # bug; if mistral-nemo fails repeatedly, the prompt needs a
    # rewrite (surfaces here as a test failure forcing supervision).
    assert fixed_run["exec_succeeded"], (
        f"bug_fix_write did not produce a passing fix on first "
        f"attempt. broken_code: {broken_code[:200]!r}. "
        f"fixed_code: {fixed_code[:200]!r}. "
        f"failing assertion stderr: {fixed_run['stderr'][:300]!r}"
    )


@pytest.mark.skipif(not _llm_reachable(), reason=SKIP_LLM)
def test_documentation_workflow_on_ollama_authored_bare_code(tmp_path):
    """Two-stage test:
    Stage 1: Ollama authors bare add(a, b) code (no docstring).
    Stage 2: code_documentation_workflow drives Ollama to add a
             Google-style docstring; we verify (a) docstring
             present + (b) function body AST unchanged.
    """
    from apecx_integration.composition.steps.code_write_step import (
        CodeWriteStep,
    )

    # STAGE 1: Ollama writes bare add(). The default CodeWriteStep
    # system prompt says "no comments narrating what you're doing"
    # which also discourages docstrings — so the default writer
    # tends to produce bare functions, perfect for this test.
    code_write = CodeWriteStep.from_config(str(CODE_WRITING_DIR / "steps" / "code_write.yml"))
    spec = (
        "Write a function add(a: int, b: int) -> int that returns "
        "a + b. For non-int inputs raise TypeError with a message "
        "naming the offending argument."
    )
    bare = asyncio.run(
        code_write.process(
            {
                "code_spec": spec,
                "function_name": "add",
                "function_signature": "def add(a: int, b: int) -> int",
            }
        )
    )
    bare_code = bare["code_source"]
    print(f"\n[doc stage 1] bare_code={bare_code[:200]!r}")
    # Confirm Ollama's bare code parses and has no docstring (or a
    # trivial one). Some models include a one-line docstring even
    # when told not to; that's fine — the doc step will REWRITE
    # it.
    initial_body_sig = _ast_body_signature(bare_code, "add")
    assert initial_body_sig, "bare code missing add() body"

    # STAGE 2: documentation step.
    code_document_write = CodeWriteStep.from_config(
        str(CODE_WRITING_DIR / "steps" / "code_document_write.yml")
    )
    start = time.monotonic()
    documented = asyncio.run(
        code_document_write.process(
            {
                "code_spec": spec,
                "function_name": "add",
                "function_signature": "def add(a: int, b: int) -> int",
                "previous_attempt": bare_code,
            }
        )
    )
    elapsed = time.monotonic() - start
    documented_code = documented["code_source"]
    print(
        f"\n[doc stage 2] elapsed={elapsed:.2f}s; documented_code preview={documented_code[:300]!r}"
    )

    # Soft gate: documentation present.
    docstring = _has_docstring(documented_code, "add")
    assert docstring, (
        f"documented code has no docstring on add(); documented_code: {documented_code[:300]!r}"
    )
    assert len(docstring.strip()) > 10, f"docstring too short to be useful: {docstring!r}"

    # Strict gate: function body AST unchanged (modulo docstring).
    documented_body_sig = _ast_body_signature(documented_code, "add")
    assert documented_body_sig == initial_body_sig, (
        f"documentation step CHANGED the function body — "
        f"behavior-preservation violation.\n"
        f"  bare body sig    : {initial_body_sig}\n"
        f"  documented body sig: {documented_body_sig}\n"
        f"  bare code: {bare_code[:300]!r}\n"
        f"  documented code: {documented_code[:300]!r}"
    )
