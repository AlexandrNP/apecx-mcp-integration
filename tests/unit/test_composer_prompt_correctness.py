"""Pin the substantive content of the composer's system prompt.

The companion ``test_composer_prompts_are_files.py`` enforces the
"prompts must be files, never inline" rule. This file goes one layer
deeper: it pins the **factual claims** the system prompt makes about
the framework, so a future edit can't silently introduce broken-on-
arrival workflow guidance.

Why these tests exist
---------------------
On 2026-05-05, four silent-failure bugs were found in workflows the
composer was generating:

  1. ``DirectLink`` defaults to ``auto_transfer=False`` → the
     workflow loads cleanly but the trigger cascade never fires.
  2. The framework's integrity validator requires workflow-level
     ``input_data_units`` / ``output_data_units`` for any multi-step
     workflow, but the prompt only forbade the (different) bare
     ``data_units:`` key.
  3. The synthesis assembly step has FIVE retrieval branches now
     (with Globus added), but the prompt mentioned only three.
  4. Data unit naming examples didn't match the canonical synthesis
     pipeline names.

These tests encode each lesson so a regression in the prompt fails
loudly at unit-test time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PROMPTS_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "apecx_integration"
    / "composition"
    / "composer_prompts"
)
SYSTEM_MD = PROMPTS_DIR / "system.md"


@pytest.fixture(scope="module")
def system_prompt() -> str:
    assert SYSTEM_MD.is_file(), SYSTEM_MD
    return SYSTEM_MD.read_text(encoding="utf-8")


def test_prompt_mandates_auto_transfer_true_on_directlinks(system_prompt):
    """Every DirectLink emitted by the composer MUST set
    ``auto_transfer: true``. The framework default is False, which
    makes the link a runtime no-op. Bug #1 from the 2026-05-05
    debugging session."""
    assert "auto_transfer: true" in system_prompt, (
        "system.md must mandate ``auto_transfer: true`` on every "
        "DirectLink. Without it, the composer generates workflows "
        "that load cleanly but no-op at runtime — silent failure."
    )
    # The mandate must be EXPLICIT — not just "auto_transfer mentioned".
    assert "REQUIRED" in system_prompt or "must" in system_prompt.lower(), (
        "the auto_transfer rule must be phrased as a hard requirement"
    )


def test_prompt_distinguishes_workflow_data_units_from_step_data_units(system_prompt):
    """Workflow-level ``input_data_units:`` and ``output_data_units:``
    blocks are REQUIRED for multi-step workflows; the bare top-level
    ``data_units:`` key is FORBIDDEN. Bug #2 from the 2026-05-05
    debugging session — prompt forbade the latter without surfacing
    the former, so the composer-generated workflows failed the
    integrity validator at initialize() time."""
    text = system_prompt
    assert "input_data_units:" in text and "output_data_units:" in text, (
        "system.md must explicitly mention workflow-level "
        "input_data_units: and output_data_units: blocks"
    )
    assert "REQUIRED" in text or "MUST" in text or "must" in text.lower(), (
        "the workflow-data-units rule must be phrased as a requirement"
    )


def test_prompt_documents_all_five_retrieval_branches(system_prompt):
    """The synthesis pipeline's assembly step runs FIVE retrieval
    branches concurrently: domain RAG, VIOLIN/BV-BRC, PubMed, Globus
    Search, and any future addition. The prompt's multi-source
    pattern section must list them so the composer doesn't omit
    Globus from the bundle shape it documents."""
    text = system_prompt.lower()
    # Each retrieval source must appear at least once in the prompt.
    assert "domain-rag" in text or "domain rag" in text or "faiss" in text
    assert "violin" in text and "bv-brc" in text
    assert "pubmed" in text
    assert "globus" in text, (
        "system.md must mention Globus Search as a retrieval branch "
        "in the synthesis pipeline. Without this, the composer's "
        "bundle-shape description would lie about what's available."
    )


def test_prompt_documents_correct_bundle_keys(system_prompt):
    """The bundle dict that ``synthesis_input`` expects has 6 keys
    after the Globus integration. The prompt must list them so the
    composer doesn't author a TransformLink stub assuming the
    pre-Globus 5-key shape."""
    bundle_keys = (
        "query",
        "rag_chunks",
        "bvbrc_genomes",
        "violin_mappings",
        "publications",
        "globus_results",
    )
    for key in bundle_keys:
        assert key in system_prompt, (
            f"bundle key {key!r} not documented in system.md — the "
            "composer needs to know the bundle's exact shape to "
            "decide whether to use SynthesisContextAssemblyStep or "
            "to author a novel fan-in step"
        )


def test_prompt_data_unit_naming_examples_match_canonical_pipeline(system_prompt):
    """The naming examples should reflect the actual synthesis-pipeline
    data unit names (assembly_input, synthesis_bundle_output, etc.)
    rather than invented placeholder names. Bug #4 from the
    2026-05-05 debugging session."""
    canonical = (
        "assembly_input",
        "synthesis_bundle_output",
        "synthesis_input",
        "synthesis_output",
    )
    matches = sum(1 for n in canonical if n in system_prompt)
    assert matches >= 2, (
        f"system.md should reference the canonical synthesis-pipeline "
        f"data unit names as concrete examples. Found {matches}/4 of: "
        f"{canonical!r}"
    )


def test_prompt_forbids_transformlink_explicitly(system_prompt):
    """TransformLink hallucination was the original prompt-engineering
    failure mode that triggered the strict link-class rule. Pin that
    the rule is still phrased as a hard prohibition."""
    text = system_prompt
    assert "TransformLink" in text
    # Look for the prohibition wording — "Do NOT" or similar.
    assert "Do NOT" in text or "do not" in text.lower() or "forbidden" in text.lower()


def test_prompt_says_to_emit_only_fenced_blocks_no_prose(system_prompt):
    """Output-format rule is the single most-violated guidance in
    practice. Pin its presence so a future edit can't soften it."""
    assert "fenced code block" in system_prompt or "fenced" in system_prompt.lower()
    assert (
        "no prose" in system_prompt.lower()
        or "Do not emit prose" in system_prompt
        or "do not emit prose" in system_prompt.lower()
    )
