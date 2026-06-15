"""Unit tests for ``apecx_integration.composition.steps.rag_synthesis_step
.RagSynthesisStep``.

Mock-policy parity: this file uses monkeypatched ``synthesize_response``
to avoid live LLM calls; the matching integration test
``tests/integration/test_rag_synthesis_step_against_ollama.py``
exercises the same code paths against a real Ollama daemon.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from apecx_integration.composition.runtime.execution_locus import (
    ExecutionLocus,
    get_active_locus,
    set_active_locus,
)
from apecx_integration.composition.steps.rag_synthesis_step import (
    RagSynthesisStep,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER_YAML = (
    REPO_ROOT
    / "src"
    / "apecx_integration"
    / "composition"
    / "workflows"
    / "rag_e2e_synthesis"
    / "steps"
    / "rag_synthesis.yml"
)


@pytest.fixture
def agent_locus():
    """Run a test under AGENT locus (internal-synthesis path), restoring the prior locus.

    The default locus is ``desktop`` (host synthesizes → the apecx LLM call is OMITTED), so a
    test exercising the internal ``synthesize_response`` branch must opt into ``agent``.
    """
    prior = get_active_locus()
    set_active_locus(ExecutionLocus.AGENT)
    try:
        yield
    finally:
        set_active_locus(prior)


def test_step_loads_via_from_config():
    """The wrapper YAML must validate against StepConfig (extra='forbid'
    on SynthesisConfig but RagSynthesisStepConfig allows the standard
    StepConfig fields). Loading via from_config is the canonical
    boot path; a load-time failure would prevent the step from
    appearing in any workflow."""
    assert WRAPPER_YAML.is_file(), WRAPPER_YAML
    step = RagSynthesisStep.from_config(str(WRAPPER_YAML))
    assert step.name == "rag_synthesis"


def test_process_rejects_missing_query(monkeypatch):
    """Fail-fast on missing/empty query before any synth work."""
    step = RagSynthesisStep.from_config(str(WRAPPER_YAML))
    with pytest.raises(ValueError, match="non-empty 'query'"):
        asyncio.run(step.process({"rag_chunks": [{"text": "x"}]}))


def test_process_rejects_non_dict_input():
    step = RagSynthesisStep.from_config(str(WRAPPER_YAML))
    with pytest.raises(ValueError, match="must be a dict"):
        asyncio.run(step.process("a string, not a dict"))  # type: ignore[arg-type]


def test_process_forwards_four_sources_to_synthesize_response(monkeypatch, agent_locus):
    """Verify the kwargs forwarded to synthesize_response carry every
    source. A future refactor that drops one (e.g. ``publications``)
    would silently lose data — this test pins the contract."""
    captured: dict[str, Any] = {}

    def _fake_synth(query: str, **kwargs: Any) -> str:
        captured["query"] = query
        captured["kwargs"] = kwargs
        return "fake markdown synthesis output"

    # Monkeypatch the exact symbol the step imports.
    import apecx_integration.composition.steps.rag_synthesis_step as mod

    monkeypatch.setattr(mod, "synthesize_response", _fake_synth)

    step = RagSynthesisStep.from_config(str(WRAPPER_YAML))
    out = asyncio.run(
        step.process(
            {
                "query": "what is sindbis",
                "rag_chunks": [{"text": "chunk"}],
                "bvbrc_genomes": [{"genome_id": "G1", "name": "n"}],
                "violin_mappings": [{"synonym_id": "VO_1", "canonical_term": "C"}],
                "publications": [{"doi": "10.1/x", "title": "T"}],
            }
        )
    )
    assert out == {"synthesis": "fake markdown synthesis output"}
    assert captured["query"] == "what is sindbis"
    kw = captured["kwargs"]
    assert kw["rag_chunks"] == [{"text": "chunk"}]
    assert kw["bvbrc_genomes"] == [{"genome_id": "G1", "name": "n"}]
    assert kw["violin_mappings"] == [{"synonym_id": "VO_1", "canonical_term": "C"}]
    assert kw["publications"] == [{"doi": "10.1/x", "title": "T"}]
    # config kwarg defaults to None (no override path) when the YAML
    # doesn't set ``synthesis_config_path``.
    assert kw["config"] is None


def test_process_defaults_missing_sources_to_empty_lists(monkeypatch, agent_locus):
    """A caller passing only query + bvbrc_genomes must NOT crash on
    KeyError when the synthesizer reads rag_chunks/violin/publications
    — the step substitutes empty lists."""
    captured: dict[str, Any] = {}

    def _fake_synth(query: str, **kwargs: Any) -> str:
        captured["kwargs"] = kwargs
        return "synthesis"

    import apecx_integration.composition.steps.rag_synthesis_step as mod

    monkeypatch.setattr(mod, "synthesize_response", _fake_synth)

    step = RagSynthesisStep.from_config(str(WRAPPER_YAML))
    asyncio.run(
        step.process(
            {
                "query": "Q",
                "bvbrc_genomes": [{"genome_id": "G1", "name": "n"}],
            }
        )
    )
    kw = captured["kwargs"]
    assert kw["rag_chunks"] == []
    assert kw["violin_mappings"] == []
    assert kw["publications"] == []


def test_synthesis_config_path_loaded_eagerly_at_init(tmp_path):
    """A bad path in ``synthesis_config_path`` must surface at step-
    init (not at first process() call). Catches the silent-failure
    shape where workflow boots, scientist submits a query, and only
    then sees a config error pointing at boot-time wiring."""
    # Write a NON-existent path into the wrapper to force the
    # eager-load failure.
    wrapper = tmp_path / "rag_synthesis_bad.yml"
    wrapper.write_text(
        "class: apecx_integration.composition.steps.rag_synthesis_step.RagSynthesisStep\n"
        f"name: bad_step\n"
        f"description: 'bad config path test'\n"
        f"synthesis_config_path: '{tmp_path / 'does-not-exist.yml'}'\n"
        "input_data_units:\n"
        "  synthesis_input:\n"
        "    class: nanobrain.core.data_unit.DataUnitMemory\n"
        "    name: synthesis_input\n"
        "    description: input\n"
        "    persistent: false\n"
        "output_data_units:\n"
        "  synthesis_output:\n"
        "    class: nanobrain.core.data_unit.DataUnitMemory\n"
        "    name: synthesis_output\n"
        "    description: output\n"
        "    persistent: false\n"
        "triggers:\n"
        "  - class: nanobrain.core.trigger.DataUnitChangeTrigger\n"
        "    data_unit: synthesis_input\n"
    )
    with pytest.raises(FileNotFoundError, match="does not exist"):
        RagSynthesisStep.from_config(str(wrapper))


def test_synthesis_config_typo_caught_at_step_init(tmp_path):
    """A typo in the operator's synthesis config (e.g.
    ``max_rag_chuncks: 8``) must raise via the SynthesisConfig
    extra='forbid' rule — and must surface at step-init via the
    eager-load path, not later."""
    bad_synth = tmp_path / "bad_synthesis.yml"
    bad_synth.write_text(
        "system_prompt: 'x'\nmax_rag_chuncks: 8\n"  # typo
    )
    wrapper = tmp_path / "rag_synthesis_typo.yml"
    wrapper.write_text(
        "class: apecx_integration.composition.steps.rag_synthesis_step.RagSynthesisStep\n"
        "name: typo_step\n"
        "description: 'typo in config'\n"
        f"synthesis_config_path: '{bad_synth}'\n"
        "input_data_units:\n"
        "  synthesis_input:\n"
        "    class: nanobrain.core.data_unit.DataUnitMemory\n"
        "    name: synthesis_input\n"
        "    description: input\n"
        "    persistent: false\n"
        "output_data_units:\n"
        "  synthesis_output:\n"
        "    class: nanobrain.core.data_unit.DataUnitMemory\n"
        "    name: synthesis_output\n"
        "    description: output\n"
        "    persistent: false\n"
        "triggers:\n"
        "  - class: nanobrain.core.trigger.DataUnitChangeTrigger\n"
        "    data_unit: synthesis_input\n"
    )
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match=r"[Ee]xtra"):
        RagSynthesisStep.from_config(str(wrapper))


def test_desktop_locus_omits_synthesis_and_defers_to_host(tmp_path, monkeypatch):
    """DESKTOP locus (default): the host LLM synthesizes, so the apecx LLM call is OMITTED.

    The step must NOT invoke synthesize_response; it returns a scaffold carrying every
    retrieved record + an instruction to the host — and works with NO apecx LLM configured.
    """
    set_active_locus(ExecutionLocus.DESKTOP)  # the default, asserted explicitly
    try:

        def _must_not_be_called(q, **k):
            raise AssertionError("synthesize_response called in desktop locus — must be omitted")

        import apecx_integration.composition.steps.rag_synthesis_step as mod

        monkeypatch.setattr(mod, "synthesize_response", _must_not_be_called)

        step = RagSynthesisStep.from_config(str(WRAPPER_YAML))
        out = asyncio.run(
            step.process(
                {
                    "query": "what is sindbis",
                    "publications": [{"doi": "10.1/x", "title": "T"}],
                    "bvbrc_genomes": [{"genome_id": "G1", "name": "n"}],
                    "rag_chunks": [{"text": "a chunk about fusion", "source": "corpus"}],
                }
            )
        )
        md = out["synthesis"]
        assert md.startswith("# Answer")
        assert "Synthesis is deferred to you" in md
        assert "10.1/x" in md  # publication carried for the host
        assert "[BV-BRC genome G1]" in md
        assert "[RAG chunk #1]" in md
    finally:
        set_active_locus(ExecutionLocus.DESKTOP)


def test_desktop_locus_empty_retrieval_says_so(tmp_path, monkeypatch):
    """DESKTOP + nothing retrieved: scaffold is honest about the empty result (loud, not an
    empty answer) and still omits the LLM call."""
    set_active_locus(ExecutionLocus.DESKTOP)
    try:
        import apecx_integration.composition.steps.rag_synthesis_step as mod

        monkeypatch.setattr(
            mod,
            "synthesize_response",
            lambda q, **k: (_ for _ in ()).throw(AssertionError("must not call LLM")),
        )
        step = RagSynthesisStep.from_config(str(WRAPPER_YAML))
        out = asyncio.run(step.process({"query": "obscure query with no hits"}))
        assert "No evidence was retrieved" in out["synthesis"]
    finally:
        set_active_locus(ExecutionLocus.DESKTOP)
