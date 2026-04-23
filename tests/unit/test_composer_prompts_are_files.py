"""AC6 enforcement: all composer prompt text lives in
``src/apecx_integration/composition/composer_prompts/*.md``, never
inline inside ``composer.py``.

The T-COMP spec flags "prompt drift" as the main risk of skipping
Phase 5 — if inline prompts creep back in, the hardening step has
to be redone. This test treats inlined prompts the way the workspace
treats hardcoded credentials: rejected at commit time.

Strategy
--------
AST-walk ``composer.py``, collect every non-docstring string literal,
and fail if any look "prompt-like" by either of two signals:

1. The literal is longer than ``MAX_LITERAL_CHARS``. Code constants
   and error messages rarely exceed this; paragraphs of prose always
   do.
2. The literal starts with the canonical system-prompt opener
   "You are" (case-insensitive). Every LLM system prompt in this
   repo begins that way; if we see it in composer.py, it leaked.

Docstrings are explicitly allowed — they document code to humans,
not instruct an LLM.
"""

from __future__ import annotations

import ast
from pathlib import Path

COMPOSER_PATH: Path = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "apecx_integration"
    / "composition"
    / "composer.py"
)

MAX_LITERAL_CHARS: int = 400


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node,
            ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        ):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                ids.add(id(first.value))
    return ids


def _looks_like_prompt(text: str) -> bool:
    if len(text) > MAX_LITERAL_CHARS:
        return True
    stripped = text.lstrip()
    return stripped.lower().startswith("you are")


def test_composer_py_has_no_inline_prompt_text() -> None:
    source = COMPOSER_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    docstring_ids = _docstring_node_ids(tree)

    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstring_ids:
            continue
        if _looks_like_prompt(node.value):
            offenders.append((node.lineno, node.value[:80]))

    assert not offenders, (
        "AC6 violation: composer.py contains prompt-shaped string "
        "literals. Move them to "
        "src/apecx_integration/composition/composer_prompts/*.md.\n"
        + "\n".join(
            f"  line {ln}: {snippet!r}" for ln, snippet in offenders
        )
    )


def test_composer_prompts_dir_has_the_expected_files() -> None:
    prompts_dir = COMPOSER_PATH.parent / "composer_prompts"
    assert prompts_dir.is_dir(), f"missing {prompts_dir}"
    expected = {"system.md", "composition_bias.md", "novel_python_flagging.md"}
    present = {p.name for p in prompts_dir.iterdir() if p.is_file()}
    missing = expected - present
    assert not missing, f"missing prompt files: {missing}"


def test_composer_prompt_files_are_non_empty() -> None:
    prompts_dir = COMPOSER_PATH.parent / "composer_prompts"
    for md in prompts_dir.glob("*.md"):
        content = md.read_text(encoding="utf-8").strip()
        assert len(content) >= 50, (
            f"prompt file {md.name} is suspiciously short ({len(content)} "
            "chars) — may be a placeholder stub"
        )
