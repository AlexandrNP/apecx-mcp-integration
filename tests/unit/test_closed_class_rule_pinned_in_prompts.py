"""Pin the CLOSED-CLASS RULE marker in every authoring-side prompt.

A future "let me clean up the prompts" edit must not silently drop
the closed-class rule. This test grep-pins the marker phrase
``CLOSED-CLASS RULE`` in every prompt file where the rule is
load-bearing.

The rule itself (paraphrased): an LLM authoring code or YAML for
the composer MUST NOT modify an existing library class or its
wrapper YAML. New behavior goes in a NEW class file with a NEW class
path referenced from the workflow YAML. Adoption requires every
existing workflow to keep working after a new one ships.

Pinned files (one marker per file is sufficient — placement guidance
lives in each prompt's tailored variant):

  composer side  (authoring workflow YAMLs / novel Python):
    composer_prompts/system.md
    composer_prompts/composition_bias.md

  code-writing side  (authoring individual functions):
    code_writing_prompts/bug_fixer_system.md
    code_writing_prompts/code_documenter_system.md
    code_writing_prompts/code_reviewer_system.md
    code_writing_prompts/code_writer_system.md
    code_writing_prompts/test_writer_system.md

Deliberately EXCLUDED:
  code_writing_prompts/workflow_summarizer_system.md
    — this prompt describes workflows for human readers, it does
    not author code or YAML.
  composer_prompts/spec_system.md
    — produces natural-language task specs, not code or YAML, so
    the rule does not apply to its output shape.
  composer_prompts/novel_python_flagging.md
    — explicitly about flagging novel Python; the rule is about
    where novel Python lands, not whether it's flagged.
  composer_prompts/reviewer_system.md
    — reviews the composer's output structurally; the closed-class
    discipline is enforced by the *authoring* prompts upstream.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = REPO_ROOT / "src" / "apecx_integration" / "composition"


PINNED_FILES = [
    PROMPT_DIR / "composer_prompts" / "system.md",
    PROMPT_DIR / "composer_prompts" / "composition_bias.md",
    PROMPT_DIR / "code_writing_prompts" / "bug_fixer_system.md",
    PROMPT_DIR / "code_writing_prompts" / "code_documenter_system.md",
    PROMPT_DIR / "code_writing_prompts" / "code_reviewer_system.md",
    PROMPT_DIR / "code_writing_prompts" / "code_writer_system.md",
    PROMPT_DIR / "code_writing_prompts" / "test_writer_system.md",
]

MARKER = "CLOSED-CLASS RULE"


@pytest.mark.parametrize(
    "prompt_path",
    PINNED_FILES,
    ids=lambda p: str(p.relative_to(REPO_ROOT)),
)
def test_closed_class_rule_marker_is_present(prompt_path: Path):
    """The marker phrase MUST appear at least once in each pinned prompt.

    If you intentionally remove the rule from one of these prompts,
    delete the corresponding row from PINNED_FILES in the same PR
    and document the reason in the PR description. Silent removal
    is what this test prevents.
    """
    assert prompt_path.exists(), f"pinned prompt missing: {prompt_path}"
    body = prompt_path.read_text(encoding="utf-8")
    assert MARKER in body, (
        f"{prompt_path.relative_to(REPO_ROOT)} no longer contains "
        f"the {MARKER!r} marker phrase. If this removal was "
        f"intentional, update PINNED_FILES in this test and document "
        f"the reason. Adoption of the apecx composer depends on the "
        f"closed-class discipline being load-bearing in every "
        f"authoring-side prompt."
    )


def test_rule_states_the_new_file_remedy():
    """The rule is incomplete without the remedy. Every pinned prompt
    must say what to do INSTEAD of editing — namely, author a NEW
    class / NEW file. Without the remedy the LLM sees a prohibition
    and may stall; with the remedy it has a clear escape hatch.
    """
    # The remedy phrasing varies by prompt but a stem must appear.
    remedy_stems = ("NEW class", "NEW file", "new function", "new class")
    for prompt_path in PINNED_FILES:
        body = prompt_path.read_text(encoding="utf-8")
        rule_block_idx = body.find(MARKER)
        assert rule_block_idx >= 0  # covered by the test above
        # Search the 1500 chars following the marker for the remedy stem
        # (the rule block is shorter than that in every prompt).
        rule_window = body[rule_block_idx : rule_block_idx + 1500]
        assert any(stem in rule_window for stem in remedy_stems), (
            f"{prompt_path.relative_to(REPO_ROOT)}: rule block has no "
            f"remedy clause. Add one of {remedy_stems!r} so the LLM "
            f"has a concrete alternative to the prohibited edit."
        )
