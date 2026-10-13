"""Code-rendered attribution for LiteratureRagStep (Lever 4): helpers, chunk source, relabelled
anchors, the References section, the desktop evidence header, and the synthesis prompt.

Toy records only (no scientific content). The backend-path tests stub ``synthesize_response``
(the LLM call); the SAME relabel/References path runs against a REAL Ollama in
``tests/integration/test_literature_rag_attribution_against_ollama.py`` (mock/integration parity).
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import yaml

import apecx_integration.composition.workflows.literature_rag.steps.literature_rag_step as mod
from apecx_integration.composition.workflows.literature_rag.steps.literature_rag_step import (
    LiteratureRagStep,
    _attribution_short,
    _pmid_anchor,
    _reference_line,
)

_OLD = {
    "pmid": "100",
    "title": "Banana toy one.",
    "abstract": "banana alpha",
    "authors": ["Older, A", "Second, B"],
    "year": "2001",
    "journal": "Toy J",
}
_NEW = {
    "pmid": "200",
    "title": "Banana toy two",
    "abstract": "banana beta",
    "authors": ["Newer, C"],
    "year": "2020",
}
_BARE = {"pmid": "300", "title": "Banana toy three", "abstract": "banana gamma"}


def test_attribution_short_formats_present_fields():
    assert _attribution_short(_OLD) == "Older et al. | 2001"
    assert _attribution_short(_NEW) == "Newer | 2020"
    assert _attribution_short({"authors": ["Solo Name"]}) == "Solo Name"
    assert _attribution_short({"year": "1999"}) == "1999"


def test_attribution_short_never_fabricates():
    assert _attribution_short(_BARE) == ""
    assert _attribution_short({"authors": [], "year": ""}) == ""
    # The container_to_dict truncation marker is a signal ("et al."), never an author.
    capped = {"authors": ["Lead, X", "et al. (30 more)"]}
    assert _attribution_short(capped) == "Lead et al."
    assert _attribution_short({"authors": ["et al. (3 more)"]}) == ""


def test_pmid_anchor_bare_when_no_attribution():
    assert _pmid_anchor("100", _OLD) == "[PMID:100 | Older et al. | 2001]"
    assert _pmid_anchor("300", _BARE) == "[PMID:300]"


def test_reference_line_full_and_partial():
    assert _reference_line(_OLD) == "Older, A; Second, B (2001). Banana toy one. Toy J. PMID:100"
    assert _reference_line(_BARE) == "Banana toy three. PMID:300"
    many = {"authors": ["A, a", "B, b", "C, c", "D, d"], "pmid": "9"}
    assert _reference_line(many) == "A, a; B, b; C, c; et al. PMID:9"


def test_reference_line_omits_absent_fields():
    line = _reference_line({"pmid": "5", "year": "2010"})
    assert line == "(2010). PMID:5"
    assert "None" not in _reference_line({"pmid": "6", "journal": None, "authors": None})


def _backend_step(monkeypatch, answer: str, captured: dict) -> LiteratureRagStep:
    import apecx_integration.composition.runtime.execution_locus as loc
    import apecx_integration.composition.workflows.literature_rag.steps as pkg

    monkeypatch.setattr(loc, "_ACTIVE_LOCUS", loc.ExecutionLocus.AGENT)
    monkeypatch.setattr("apecx_integration.agents._llm_config.preflight_llm_model", lambda: None)
    monkeypatch.setattr("apecx_integration.agents._llm_factory.build_chat_llm", lambda: object())

    def _fake_synth(question, *, rag_chunks, llm, config):
        captured["chunks"] = rag_chunks
        return answer

    monkeypatch.setattr(mod, "synthesize_response", _fake_synth)
    for flag in (
        "APECX_ENABLE_LLM_VERIFY",
        "APECX_ENABLE_CROSS_ENCODER",
        "APECX_ENABLE_LLM_FUSION",
    ):
        monkeypatch.delenv(flag, raising=False)
    step = LiteratureRagStep.from_config(
        str(Path(pkg.__file__).resolve().parent / "literature_rag_step.yml")
    )
    step._embed_sims = lambda question, records: [0.0] * len(records)
    return step


def _run(step, records):
    env = {"question": "Which texts mention a banana?", "filtered_records": records}
    return asyncio.run(step.process(env))


def _chunk_index_by_pmid(captured) -> dict[str, int]:
    return {c["id"]: i for i, c in enumerate(captured["chunks"], start=1)}


def test_chunk_source_carries_attribution(monkeypatch):
    captured: dict = {}
    step = _backend_step(monkeypatch, "x [RAG chunk #1]", captured)
    _run(step, [_OLD, _NEW, _BARE])
    sources = {c["id"]: c["source"] for c in captured["chunks"]}
    assert sources["100"] == "PMID:100 | Older et al. | 2001 | Toy J"
    assert sources["200"] == "PMID:200 | Newer | 2020"
    assert sources["300"] == "PMID:300"


def test_relabel_renders_enriched_anchors(monkeypatch):
    captured: dict = {}
    step = _backend_step(monkeypatch, "", captured)
    _run(step, [_OLD, _NEW, _BARE])  # learn the chunk order the step produced
    idx = _chunk_index_by_pmid(captured)
    answer = f"A [RAG chunk #{idx['100']}]. B [RAG chunk #{idx['300']}]."
    step = _backend_step(monkeypatch, answer, captured)
    out = _run(step, [_OLD, _NEW, _BARE])
    assert out["status"] == "ok"
    assert "A [PMID:100 | Older et al. | 2001]." in out["answer"]
    assert "B [PMID:300]." in out["answer"]
    assert out["citations"] == ["100", "300"]


def test_references_list_only_cited_records_chronologically(monkeypatch):
    captured: dict = {}
    step = _backend_step(monkeypatch, "", captured)
    _run(step, [_NEW, _OLD, _BARE])
    idx = _chunk_index_by_pmid(captured)
    # Cite undated first, then recent, then old: References must re-order old -> new -> undated.
    answer = (
        f"U [RAG chunk #{idx['300']}]. N [RAG chunk #{idx['200']}]. O [RAG chunk #{idx['100']}]."
    )
    step = _backend_step(monkeypatch, answer, captured)
    out = _run(step, [_NEW, _OLD, _BARE])
    body, refs = out["answer"].split("\n\n## References\n\n")
    lines = refs.splitlines()
    assert [re.search(r"PMID:(\d+)$", ln).group(1) for ln in lines] == ["100", "200", "300"]
    assert lines[0].startswith("1. Older, A; Second, B (2001)")
    assert set(out["citations"]) == {"100", "200", "300"}


def test_references_exclude_uncited_records(monkeypatch):
    captured: dict = {}
    step = _backend_step(monkeypatch, "", captured)
    _run(step, [_OLD, _NEW, _BARE])
    idx = _chunk_index_by_pmid(captured)
    step = _backend_step(monkeypatch, f"Only [RAG chunk #{idx['200']}].", captured)
    refs = _run(step, [_OLD, _NEW, _BARE])["answer"].split("## References")[1]
    assert "PMID:200" in refs and "PMID:100" not in refs and "PMID:300" not in refs


def test_desktop_evidence_header_includes_attribution():
    md = LiteratureRagStep._render_host_evidence("q?", [_OLD, _BARE])
    assert "## [PMID:100 | Older et al. | 2001] Banana toy one." in md
    assert "_Toy J_" in md
    assert "## [PMID:300] Banana toy three" in md


def _system_prompt() -> str:
    raw = yaml.safe_load(mod._SYNTHESIS_CONFIG_PATH.read_text(encoding="utf-8"))
    return raw["system_prompt"]


def test_prompt_requests_specificity_chronology_levels_of_evidence():
    prompt = _system_prompt()
    for directive in ("SPECIFICITY", "CHRONOLOGY", "LEVELS OF EVIDENCE", "PDB", "residue"):
        assert directive in prompt
    assert "[RAG chunk #N]" in prompt  # the gate's citation token is still the only one taught


def test_prompt_never_tells_llm_to_write_pmid_or_authors():
    prompt = _system_prompt()
    assert "[PMID:" not in prompt
    assert "Do NOT write PMIDs, author names, or a reference list yourself" in prompt
    from apecx_integration.agents.rag_synthesis.synthesizer import SynthesisConfig

    SynthesisConfig.model_validate(  # still a valid (extra='forbid') config
        yaml.safe_load(mod._SYNTHESIS_CONFIG_PATH.read_text(encoding="utf-8"))
    )
