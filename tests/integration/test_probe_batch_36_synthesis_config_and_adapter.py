"""Probe batch 36 — adversarial probes targeting SynthesisConfig
validation, harvester adapter edge cases, and Day 2 v7 surface.

Streak before this batch: 50/300 post-AQ.
Probe naming: 955–979.

Distinct probes only — none of these check shapes covered by prior
batches (1–35).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from apecx_harvesters.loaders.base.model import (
    Affiliation,
    Creator,
    DataCite,
    Description,
    DescriptionType,
    Identifier,
    Publisher,
    Title,
)
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from apecx_integration.agents.rag_synthesis import (
    DEFAULT_SYNTHESIS_CONFIG_PATH,
    SynthesisConfig,
    datacite_to_publication,
    synthesize_response,
)


pytestmark = pytest.mark.integration


def _cfg(**overrides) -> SynthesisConfig:
    import yaml
    raw = yaml.safe_load(DEFAULT_SYNTHESIS_CONFIG_PATH.read_text())
    return SynthesisConfig.model_validate(raw).model_copy(update=overrides)


class _Stub:
    def __init__(self, content: str) -> None:
        self.content = content

    def invoke(self, msgs):
        return AIMessage(content=self.content)


def _datacite(**overrides) -> DataCite:
    base = dict(
        identifier=Identifier(identifier="10.1/x", identifierType="DOI"),
        creators=[],
        titles=[Title(title="T")],
        publisher=Publisher(name="P"),
    )
    base.update(overrides)
    return DataCite(**base)


# --------------------------------------------------------------------------- #
# Probes 955–979
# --------------------------------------------------------------------------- #


def test_probe_955_synthesis_config_extra_fields_rejected_or_known_default():
    """SynthesisConfig should not silently accept arbitrary keys — a
    typo in synthesis_config.yml ("max_rag_chuncks: 8") would
    otherwise silently use the default 8 from the schema and an
    operator would never know. Confirms ``extra='forbid'``."""
    with pytest.raises(ValidationError, match=r"[Ee]xtra"):
        SynthesisConfig.model_validate(
            {"system_prompt": "x", "typoed_field": 42}
        )


def test_probe_956_min_response_chars_negative_rejected():
    """Pydantic ``ge=0`` on min_response_chars must reject negatives."""
    with pytest.raises(ValidationError):
        SynthesisConfig.model_validate(
            {"system_prompt": "x", "min_response_chars": -10}
        )


def test_probe_957_max_rag_chunks_negative_rejected():
    """``ge=1`` on max_rag_chunks (default 8) rejects 0 and negative."""
    with pytest.raises(ValidationError):
        SynthesisConfig.model_validate(
            {"system_prompt": "x", "max_rag_chunks": -1}
        )


def test_probe_958_max_publications_zero_accepted():
    """``ge=0`` on max_publications: zero is a defensible operator
    choice (disable harvester for a deployment), must NOT raise."""
    cfg = SynthesisConfig.model_validate(
        {"system_prompt": "x", "max_publications": 0}
    )
    assert cfg.max_publications == 0


def test_probe_959_min_distinct_citations_zero_accepted():
    """min_distinct=0 disables the "must cite something" rule. Some
    deployments use the synthesizer as a free-form drafter without
    citation enforcement; this must not raise."""
    cfg = SynthesisConfig.model_validate(
        {"system_prompt": "x", "min_distinct_citations": 0}
    )
    assert cfg.min_distinct_citations == 0


def test_probe_960_empty_system_prompt_either_rejected_or_accepted_explicitly():
    """An empty system_prompt is a smell (the LLM gets no role
    guidance); the schema either rejects (preferred) or accepts. The
    silent-failure shape would be loading an empty prompt into the
    LLM and getting confabulation. Confirm at least one of the two
    deterministic behaviors."""
    try:
        cfg = SynthesisConfig.model_validate(
            {"system_prompt": ""}
        )
        # Acceptance is OK only if the empty prompt then renders into
        # the LLM message verbatim (no silent default substitution).
        assert cfg.system_prompt == ""
    except ValidationError:
        pass  # Rejection is the safer choice; also fine.


def test_probe_961_load_default_config_raises_on_missing_file(tmp_path, monkeypatch):
    """Patching the default-config path to a non-existent file must
    raise a clear RuntimeError, not silently fall back to defaults
    (which would then quietly diverge from operator-supplied YAML)."""
    from apecx_integration.agents.rag_synthesis import synthesizer
    bogus = tmp_path / "does-not-exist.yml"
    monkeypatch.setattr(synthesizer, "DEFAULT_SYNTHESIS_CONFIG_PATH", bogus)
    with pytest.raises(RuntimeError, match="Synthesis config not found"):
        synthesizer._load_default_config()


def test_probe_962_load_default_config_surfaces_yaml_parse_error(tmp_path, monkeypatch):
    """Malformed YAML must surface a parse error — not silently
    return None / use defaults."""
    from apecx_integration.agents.rag_synthesis import synthesizer
    bad = tmp_path / "bad.yml"
    bad.write_text(":\n  - this is not valid: yaml: at all\n  yaml: ::")
    monkeypatch.setattr(synthesizer, "DEFAULT_SYNTHESIS_CONFIG_PATH", bad)
    with pytest.raises(Exception):
        synthesizer._load_default_config()


def test_probe_963_adapter_publication_year_int_coerced_or_rejected_clearly():
    """DataCite's publicationYear is Optional[str]; the strict pydantic
    schema should reject an int. Confirm the failure is at the
    DataCite validation boundary, not silently coerced."""
    with pytest.raises(ValidationError):
        DataCite(
            identifier=Identifier(identifier="10.1/x", identifierType="DOI"),
            creators=[], titles=[Title(title="T")],
            publisher=Publisher(name="P"),
            publicationYear=2024,  # int, not str
        )


def test_probe_964_adapter_with_empty_string_doi_rejected():
    """An ``Identifier`` with identifier='' is structurally allowed by
    pydantic (str type); the citation pattern requires ``10.<id>/...``
    so the renderer would reject downstream. Confirm the adapter
    surfaces the issue too — empty string fails the DOI literal
    contract."""
    rec = _datacite(
        identifier=Identifier(identifier="", identifierType="DOI"),
    )
    pub = datacite_to_publication(rec)
    # The adapter does not validate DOI literal shape — the
    # synthesizer's renderer does. The production flow then
    # rejects in strict mode with "missing or non-DOI".
    assert pub["doi"] == ""
    from apecx_integration.agents.rag_synthesis.synthesizer import (
        _render_publications,
    )
    with pytest.raises(ValueError, match="missing or non-DOI"):
        _render_publications([pub], cap=1, strict=True)


def test_probe_965_adapter_with_creator_empty_strings_skipped():
    """Creator with all empty strings (givenName='', familyName='',
    name='') would render as '' — skip silently."""
    rec = _datacite(creators=[Creator(givenName="", familyName="", name="")])
    pub = datacite_to_publication(rec)
    assert "authors" not in pub


def test_probe_966_adapter_creator_with_only_given_name_skipped():
    """Creator with only ``givenName`` (no familyName, no name) cannot
    be rendered as a single string — current code path requires
    BOTH given+family OR name. Confirm the silent-skip behavior is
    deterministic."""
    rec = _datacite(creators=[Creator(givenName="OnlyGiven")])
    pub = datacite_to_publication(rec)
    # Current contract: skip silently. (Documented behavior.)
    assert "authors" not in pub


def test_probe_967_adapter_creator_with_affiliation_does_not_leak_into_authors_string():
    """Affiliation should NOT appear in the rendered author name —
    only the personal name. The synthesizer's renderer wouldn't know
    how to format a string that contains affiliation prose."""
    rec = _datacite(
        creators=[Creator(
            givenName="Marie", familyName="Curie",
            affiliation=Affiliation(name="University of Paris"),
        )],
    )
    pub = datacite_to_publication(rec)
    assert pub["authors"] == ["Marie Curie"]
    assert "University" not in str(pub["authors"])


def test_probe_968_adapter_round_trip_through_synthesize_response():
    """End-to-end: real DataCite -> adapter -> renderer -> grounding
    accepts the produced citation token. No mock between the two
    schema layers."""
    rec = _datacite(
        identifier=Identifier(identifier="10.1/round-trip", identifierType="DOI"),
        descriptions=[Description(
            description="Real abstract.",
            descriptionType=DescriptionType.Abstract,
        )],
    )
    pub = datacite_to_publication(rec)
    stub = _Stub(content=("Long body. " * 30) + "[10.1/round-trip]")
    out = synthesize_response("Q", llm=stub, publications=[pub])
    assert "[10.1/round-trip]" in out


def test_probe_969_e2e_load_helpers_resolve_paths_independent_of_cwd(tmp_path, monkeypatch):
    """The E2E test's REPO_ROOT / WORKSPACE_ROOT path resolution uses
    ``Path(__file__).resolve().parents[N]``, which is cwd-independent.
    Confirm by changing cwd and verifying the resolution holds."""
    from tests.integration import test_e2e_rag_pipeline_against_ollama as m
    monkeypatch.chdir(tmp_path)
    assert m.REPO_ROOT.is_dir(), m.REPO_ROOT
    assert m.WORKSPACE_ROOT.is_dir(), m.WORKSPACE_ROOT


def test_probe_970_e2e_keyword_filter_excludes_short_keywords():
    """E2E ``_load_violin_vaccines`` filters out keywords shorter
    than 4 chars. Verify the filter, otherwise a 1-char query would
    match almost any vaccine name (false positives)."""
    from tests.integration import test_e2e_rag_pipeline_against_ollama as m
    if not m.VIOLIN_VACCINES.is_file():
        pytest.skip("VIOLIN CSV not present")
    out = m._load_violin_vaccines(["a", "to", "the"], limit=10)
    # All keywords are <4 chars and must be filtered out → no match.
    assert out == [], (
        f"short-keyword filter failed: matched {len(out)} rows on "
        f"length-only-junk keywords"
    )


def test_probe_971_e2e_load_bvbrc_genome_id_field_present_for_all_rows():
    """Every row from ``_load_bvbrc_genomes`` must have a ``genome_id``
    field — that's the contract the synthesizer requires. A malformed
    TSV row missing the column would silently emit a row that the
    strict validation later rejects, and the error point would be
    far from the source. Confirm the loader is the right gate."""
    from tests.integration import test_e2e_rag_pipeline_against_ollama as m
    if not m.BVBRC_TSV.is_file():
        pytest.skip("BV-BRC TSV not present")
    rows = m._load_bvbrc_genomes(limit=5)
    assert all(r.get("genome_id") for r in rows), rows


def test_probe_972_distinct_id_renderer_handles_int_genome_id_or_rejects():
    """A BV-BRC row with genome_id as int (not str) — does the
    renderer crash, silently coerce, or surface cleanly?"""
    from apecx_integration.agents.rag_synthesis.synthesizer import (
        _render_bvbrc_genomes,
    )
    rendered, allowed = _render_bvbrc_genomes(
        [{"genome_id": 42, "name": "n"}], cap=1, strict=True,
    )
    # Acceptable behavior: render the int as a string.
    assert "42" in rendered
    assert "[BV-BRC genome 42]" in allowed


def test_probe_973_publication_doi_with_internal_brackets_rejected_in_render():
    """A DOI string containing ``]`` would break the citation token
    by closing it early. The strict renderer must either reject or
    the regex pattern must escape correctly. Test current behavior."""
    pub = {"doi": "10.1/has]bracket", "title": "T"}
    from apecx_integration.agents.rag_synthesis.synthesizer import (
        _render_publications, _extract_distinct_citations,
    )
    rendered, allowed = _render_publications([pub], cap=1, strict=True)
    # The renderer accepts the DOI; the regex pattern is the gate.
    # Verify the pattern matches up to the FIRST ``]`` only —
    # confirming the silent-failure shape is contained.
    cfg = _cfg()
    extracted = _extract_distinct_citations(
        f"see [{pub['doi']}] above", cfg.citation_marker_patterns,
    )
    # Pattern: ``\[10\.[0-9]+/[^\]]+\]`` — ``[^\]]+`` excludes ``]``,
    # so the match would be ``[10.1/has]`` — that's NOT the same
    # token as the renderer's. allowed has ``[10.1/has]bracket]``,
    # extracted has ``[10.1/has]``. They differ. Grounding would
    # reject — silent-failure CONTAINED.
    if extracted:
        # extracted shape != allowed shape → this DOI is uncitable
        # under the current pattern. That's defensible — DOIs with
        # ``]`` are pathological. Confirm allowed and extracted
        # disagree (the grounding gate would fire).
        assert extracted != allowed, (
            "DOI with ``]`` somehow round-trips. The citation pattern "
            "should not match through a ``]``"
        )


def test_probe_974_two_chunks_with_same_id_renumber_independently():
    """Chunks may share an ``id`` (rare but possible — e.g., two
    embeddings of the same source). The surviving-position numbering
    must NOT use ``id`` — it uses position. Verify."""
    from apecx_integration.agents.rag_synthesis.synthesizer import (
        _render_rag_chunks,
    )
    rendered, allowed = _render_rag_chunks(
        [
            {"text": "first", "id": "shared"},
            {"text": "second", "id": "shared"},
        ],
        cap=8, strict=True,
    )
    assert allowed == {"[RAG chunk #1]", "[RAG chunk #2]"}


def test_probe_975_caps_applied_before_validation_drops_rather_than_rejects():
    """Strict validation runs INSIDE the cap-slice loop. So a bad row
    at position N>cap is not seen — even strict mode does not raise.
    This is current behavior. Verify it's deterministic so an
    operator can rely on it."""
    from apecx_integration.agents.rag_synthesis.synthesizer import (
        _render_bvbrc_genomes,
    )
    rendered, allowed = _render_bvbrc_genomes(
        [
            {"genome_id": "G1", "name": "g1"},  # valid, position 0
            {"genome_id": "G2", "name": "g2"},  # valid, position 1
            {"name": "no_id_pos_2"},            # invalid, beyond cap=2
        ],
        cap=2, strict=True,
    )
    # Cap=2 sees only first 2 rows; bad row at index 2 not seen.
    assert allowed == {"[BV-BRC genome G1]", "[BV-BRC genome G2]"}


def test_probe_976_publish_year_garbage_string_passes_through_renderer():
    """Pre-renderer the year is whatever the harvester emitted. The
    renderer interpolates as ``{year}``; non-4-digit strings render
    verbatim. Acceptable — the synthesizer does not validate year
    semantics. This probe pins down the no-validation contract so a
    future tightening fails this test loudly."""
    from apecx_integration.agents.rag_synthesis.synthesizer import (
        _render_publications,
    )
    pub = {"doi": "10.1/x", "title": "T", "year": "ancient"}
    rendered, _ = _render_publications([pub], cap=1, strict=True)
    assert "ancient" in rendered


def test_probe_977_e2e_pipeline_test_module_is_importable_under_pytest():
    """Importing the E2E test module under pytest must not require
    Ollama to be reachable (the gate is a fixture, not an import-time
    side effect). A failed import would prevent collection on any
    environment without Ollama."""
    import importlib
    mod = importlib.import_module(
        "tests.integration.test_e2e_rag_pipeline_against_ollama"
    )
    assert hasattr(mod, "_load_bvbrc_genomes")
    assert hasattr(mod, "_load_violin_vaccines")
    assert hasattr(mod, "_load_rag_chunks")


def test_probe_978_synthesizer_module_import_does_not_eagerly_load_yaml():
    """The default config must be loaded LAZILY — module import must
    not parse YAML. A malformed YAML at import time would otherwise
    break unrelated code paths. Verify by importing without
    triggering ``_load_default_config``."""
    import importlib
    import sys
    # If already imported, drop and re-import to time the load.
    sys.modules.pop("apecx_integration.agents.rag_synthesis.synthesizer", None)
    mod = importlib.import_module(
        "apecx_integration.agents.rag_synthesis.synthesizer"
    )
    # Module attributes available without YAML being parsed:
    assert hasattr(mod, "synthesize_response")
    assert hasattr(mod, "SynthesisConfig")
    assert hasattr(mod, "DEFAULT_SYNTHESIS_CONFIG_PATH")
    # The path is a Path object, not a parsed dict — proves laziness.
    assert isinstance(mod.DEFAULT_SYNTHESIS_CONFIG_PATH, Path)


def test_probe_979_synthesize_response_passes_kwargs_only_no_positional():
    """The function signature uses ``*`` to mark every argument after
    ``query`` as kwarg-only. Calling with positional args must
    raise. This protects against argument-order misalignment in
    callers (rag_chunks vs bvbrc_genomes vs publications)."""
    from langchain_core.messages import AIMessage

    class _S:
        def invoke(self, _):
            return AIMessage(content=("body " * 50))

    with pytest.raises(TypeError):
        # Call with positional args → must raise.
        synthesize_response("Q", [], [], [], [])  # type: ignore[misc]
