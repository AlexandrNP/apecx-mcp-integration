"""Pin the REUSE-FIRST RULE marker in every authoring-/reviewing-side prompt.

Parallel to ``test_closed_class_rule_pinned_in_prompts.py``: a future
"trim the prompts" edit must not silently drop the reuse-first rule.
This test grep-pins the marker phrase ``REUSE-FIRST RULE`` in every
prompt where the rule is load-bearing.

The rule itself (paraphrased): before authoring NEW code (functions,
classes, novel_python steps, custom assertions), check whether an
existing library component, stdlib utility, or pytest feature already
covers the task. Prefer reuse. Adoption depends on the library being
the source of truth.

Pinned files — same set as the closed-class rule PLUS the composer-
side reviewer (which catches unjustified novel_python at the workflow
level):

  composer side:
    composer_prompts/system.md
    composer_prompts/composition_bias.md
    composer_prompts/reviewer_system.md           (NEW vs. closed-class set)

  code-writing side:
    code_writing_prompts/bug_fixer_system.md
    code_writing_prompts/code_documenter_system.md
    code_writing_prompts/code_reviewer_system.md
    code_writing_prompts/code_writer_system.md
    code_writing_prompts/test_writer_system.md

Deliberately EXCLUDED:
  code_writing_prompts/workflow_summarizer_system.md
    — describes workflows; doesn't author or review code.
  composer_prompts/spec_system.md
    — produces task specs, not code or YAML.
  composer_prompts/novel_python_flagging.md
    — about flagging structure of novel_python; the reuse rule sits
      upstream (in system.md + composition_bias.md) and downstream
      (in reviewer_system.md).
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = REPO_ROOT / "src" / "apecx_integration" / "composition"


PINNED_FILES = [
    PROMPT_DIR / "composer_prompts" / "system.md",
    PROMPT_DIR / "composer_prompts" / "composition_bias.md",
    PROMPT_DIR / "composer_prompts" / "reviewer_system.md",
    PROMPT_DIR / "code_writing_prompts" / "bug_fixer_system.md",
    PROMPT_DIR / "code_writing_prompts" / "code_documenter_system.md",
    PROMPT_DIR / "code_writing_prompts" / "code_reviewer_system.md",
    PROMPT_DIR / "code_writing_prompts" / "code_writer_system.md",
    PROMPT_DIR / "code_writing_prompts" / "test_writer_system.md",
]

MARKER = "REUSE-FIRST RULE"


@pytest.mark.parametrize(
    "prompt_path",
    PINNED_FILES,
    ids=lambda p: str(p.relative_to(REPO_ROOT)),
)
def test_reuse_first_rule_marker_is_present(prompt_path: Path):
    """The marker phrase MUST appear at least once in each pinned prompt.

    If you intentionally remove the rule from one of these prompts,
    delete the corresponding row from PINNED_FILES in the same PR
    and document the reason. Silent removal is what this test prevents.
    """
    assert prompt_path.exists(), f"pinned prompt missing: {prompt_path}"
    body = prompt_path.read_text(encoding="utf-8")
    assert MARKER in body, (
        f"{prompt_path.relative_to(REPO_ROOT)} no longer contains "
        f"the {MARKER!r} marker phrase. If this removal was "
        f"intentional, update PINNED_FILES in this test and document "
        f"the reason. Adoption of the apecx composer depends on the "
        f"reuse-first discipline being load-bearing in every "
        f"authoring/reviewing prompt."
    )


def test_rule_states_concrete_reuse_targets():
    """The rule is meaningless without naming WHAT to reuse. Every
    pinned prompt must mention at least one concrete reuse target so
    the LLM has a starting point rather than an abstract injunction.

    Acceptable concrete targets — at least one must appear in the
    rule's vicinity (within 1500 chars of the marker):
    """
    reuse_targets = (
        # stdlib modules
        "itertools",
        "collections",
        "functools",
        "statistics",
        "pathlib",
        "Counter",
        # builtins
        "sum(",
        "max(",
        "min(",
        "sorted(",
        # pytest features
        "pytest.raises",
        "pytest.approx",
        "pytest.mark.parametrize",
        # library components
        "library component",
        "library candidate",
        "candidate-components",
        "CodeReflectionStep",
        "CodeVerificationStep",
        "SynthesisContextAssemblyStep",
    )
    for prompt_path in PINNED_FILES:
        body = prompt_path.read_text(encoding="utf-8")
        rule_block_idx = body.find(MARKER)
        assert rule_block_idx >= 0  # covered above
        rule_window = body[rule_block_idx : rule_block_idx + 1500]
        assert any(target in rule_window for target in reuse_targets), (
            f"{prompt_path.relative_to(REPO_ROOT)}: rule block names "
            f"no concrete reuse target. Add at least one of "
            f"{reuse_targets!r} near the marker, otherwise the LLM "
            f"sees a vague injunction with no starting point."
        )
