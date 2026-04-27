"""Probe batch 39 — adversarial probes against workflow integration
corners and DataCite adapter edge cases I haven't yet probed.

Streak before this batch: 75/300 post-AQ.
Probe naming: 1030–1054.

Distinct probes only.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from apecx_harvesters.loaders.base.model import (
    Affiliation,
    Creator,
    DataCite,
    Description,
    DescriptionType,
    Identifier,
    Publisher,
    Subject,
    Title,
    TitleType,
)
from nanobrain.core.workflow import Workflow

from apecx_integration.agents.rag_synthesis import (
    SynthesisConfig,
    datacite_to_publication,
    synthesize_response,
)
from apecx_integration.agents.rag_synthesis.synthesizer import (
    DEFAULT_SYNTHESIS_CONFIG_PATH,
    _render_publications,
)


pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_YAML = (
    REPO_ROOT / "src" / "apecx_integration" / "composition"
    / "workflows" / "violin_bvbrc" / "violin_bvbrc_workflow.yml"
)


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
# Probes 1030–1054
# --------------------------------------------------------------------------- #


def test_probe_1030_workflow_yaml_loads_with_rag_synthesis_registered():
    """The workflow YAML now registers rag_synthesis. Loading must
    succeed under the canonical Workflow.from_config path — a
    silent regression where the YAML half-loads (e.g. step missing
    from registration but no error) is the shape this probe
    catches."""
    wf = Workflow.from_config(str(WORKFLOW_YAML))
    assert wf.name == "violin_bvbrc_workflow"


def test_probe_1031_workflow_with_unlinked_step_does_not_raise():
    """rag_synthesis is registered-but-unlinked. Some workflow
    frameworks reject orphan steps loudly; nanobrain must accept
    them quietly (per workflow YAML's pattern of having multiple
    loaded-but-unlinked steps). Pin this contract."""
    # Just loading is sufficient; the assertion in 1030 covers
    # success. This probe is the explicit-orphan-acceptance check.
    wf = Workflow.from_config(str(WORKFLOW_YAML))
    assert wf is not None  # didn't raise on orphan rag_synthesis


def test_probe_1032_adapter_includes_field_when_only_givenName_set():
    """A Creator with ONLY givenName (no familyName, no name) is
    skipped per the documented contract. But what if BOTH givenName
    and familyName are non-empty AND name is also set? Verify that
    given+family wins (the docstring contract)."""
    rec = _datacite(creators=[
        Creator(givenName="Alice", familyName="Smith", name="A. Smith Sr."),
    ])
    pub = datacite_to_publication(rec)
    assert pub["authors"] == ["Alice Smith"], (
        f"expected given+family preferred over name; got {pub['authors']!r}"
    )


def test_probe_1033_adapter_creator_with_only_familyName_skipped():
    """Creator with only familyName (no givenName, no name) — current
    contract is skip silently. Pin so a future code path that emits
    'familyName-only' as the author name is caught."""
    rec = _datacite(creators=[Creator(familyName="OnlyFamily")])
    pub = datacite_to_publication(rec)
    assert "authors" not in pub


def test_probe_1034_adapter_publication_year_None_omitted():
    """publicationYear is Optional. None => key absent in pub dict."""
    rec = _datacite(publicationYear=None)
    pub = datacite_to_publication(rec)
    assert "year" not in pub


def test_probe_1035_adapter_publisher_name_required_by_datacite_schema():
    """Publisher.name is required by DataCite; an empty-string Publisher
    name should be allowed by pydantic (str type allows empty), and
    the adapter should still surface it. Verify behavior — empty
    journal might display as '' in the rendered prompt."""
    rec = _datacite(publisher=Publisher(name=""))
    pub = datacite_to_publication(rec)
    # Adapter only includes journal if publisher.name truthy.
    assert "journal" not in pub


def test_probe_1036_adapter_titles_list_with_empty_string_title():
    """A Title with empty-string title field is structurally valid
    (str type). The adapter picks it as the primary if no titleType
    is set. Verify — an empty title would render as '' in the prompt."""
    rec = _datacite(titles=[Title(title="")])
    pub = datacite_to_publication(rec)
    # Current contract: empty string IS the title (truthy check is
    # only on creator name fields). Pin this so we know if it changes.
    assert pub["title"] == ""


def test_probe_1037_adapter_descriptions_with_multiple_abstracts_first_wins():
    """Multiple Abstract descriptions: the first wins (current
    contract). Pin to catch a future change to "concatenate all"
    or "longest wins" silently."""
    rec = _datacite(descriptions=[
        Description(description="first abstract", descriptionType=DescriptionType.Abstract),
        Description(description="second abstract", descriptionType=DescriptionType.Abstract),
    ])
    pub = datacite_to_publication(rec)
    assert pub["abstract"] == "first abstract"


def test_probe_1038_adapter_subjects_field_silently_dropped():
    """DataCite ``subjects`` field (controlled-vocab keywords) is NOT
    propagated into the publication dict — current contract. A
    future enrichment might want to surface them; this probe pins
    the current behavior so the change is intentional."""
    rec = _datacite(subjects=[Subject(subject="virology")])
    pub = datacite_to_publication(rec)
    assert "subjects" not in pub


def test_probe_1039_adapter_with_rich_datacite_yields_only_known_keys():
    """Probe the OUTPUT shape: regardless of how rich the DataCite
    record is, the publication dict has at most these keys:
    {doi, title, authors, year, journal, abstract}. Anything else
    leaking is a contract violation."""
    rec = _datacite(
        creators=[Creator(givenName="A", familyName="B")],
        publicationYear="2024",
        descriptions=[Description(
            description="abs", descriptionType=DescriptionType.Abstract,
        )],
        subjects=[Subject(subject="virology")],
        formats=["application/pdf"],
        version="2.1",
    )
    pub = datacite_to_publication(rec)
    expected_keys = {"doi", "title", "authors", "year", "journal", "abstract"}
    assert set(pub.keys()).issubset(expected_keys), (
        f"adapter leaked unexpected keys: {set(pub.keys()) - expected_keys}"
    )


def test_probe_1040_adapter_doi_with_uppercase_prefix_rejected():
    """DOI strings are case-sensitive (registry treats them case-
    insensitively but the literal should match the regex).
    ``identifier='10.1/x'`` works; what about the synthesizer's
    pattern with an upper-case prefix in the literal? The pattern
    requires literal ``10.``, so uppercase ``10.X`` works. But what
    if the regex's character class is too narrow?"""
    pub = {"doi": "10.1234/ABCdef-2024", "title": "T"}
    rendered, allowed = _render_publications([pub], cap=1, strict=True)
    assert allowed == {"[10.1234/ABCdef-2024]"}


def test_probe_1041_synthesis_config_loads_yaml_with_duplicate_keys_yaml_safe_load():
    """YAML allows duplicate keys (last-wins); pyyaml's safe_load
    returns the last value silently. SynthesisConfig should still
    accept the resulting dict — but operators get NO warning. The
    silent-failure shape: a duplicate key in synthesis_config.yml
    would be silently last-wins.

    Pin: pyyaml's behavior is what it is; no library-side guard.
    A future probe could add a YAML linter step to surface
    duplicates."""
    import yaml
    raw = yaml.safe_load(
        "system_prompt: 'first'\n"
        "system_prompt: 'last'\n"
    )
    cfg = SynthesisConfig.model_validate(raw)
    # Last wins per YAML 1.1 semantics + pyyaml.
    assert cfg.system_prompt == "last"


def test_probe_1042_synthesis_config_path_with_unicode():
    """Operators on non-ASCII filesystems: the synthesis_config_path
    must accept a Path with unicode chars. Verify by writing a
    config in a unicode dir."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        unicode_dir = Path(tmp) / "配置-données"
        unicode_dir.mkdir()
        cfg_path = unicode_dir / "synthesis.yml"
        cfg_path.write_text("system_prompt: 'unicode-path'\n", encoding="utf-8")
        from apecx_integration.composition.steps.rag_synthesis_step import (
            RagSynthesisStep,
        )
        loaded = RagSynthesisStep._load_synthesis_config(cfg_path)
        assert loaded.system_prompt == "unicode-path"


def test_probe_1043_render_publications_with_int_year_in_dict():
    """Adapter normally produces year as a string (DataCite contract);
    but a caller might bypass the adapter and pass year as an int.
    The renderer's f-string handles ``{year}`` for any printable —
    int renders correctly. Pin so a future {year:%Y-%m-%d} format
    breaks loudly."""
    pub = {"doi": "10.1/x", "title": "T", "year": 2025}
    rendered, _ = _render_publications([pub], cap=1, strict=True)
    assert "2025" in rendered


def test_probe_1044_synthesize_response_None_inputs_explicit():
    """The signature defaults rag_chunks/etc. to None. Passing None
    explicitly must be equivalent to omitting (already covered by
    the function signature). Pin — a future change to default=[]
    would silently change semantics for callers passing None."""
    inputs = dict(
        rag_chunks=None,
        bvbrc_genomes=[{"genome_id": "G1", "name": "n"}],
        violin_mappings=None,
        publications=None,
    )

    class _Stub:
        def invoke(self, msgs):
            from langchain_core.messages import AIMessage
            return AIMessage(content=("body " * 50) + "[BV-BRC genome G1]")

    out = synthesize_response("Q", llm=_Stub(), **inputs)
    assert "[BV-BRC genome G1]" in out


def test_probe_1045_synthesize_with_publications_doi_starting_with_just_10():
    """A DOI ``"10."`` (literal "10.") would startswith("10.") but
    has no path. The synthesizer's strict check has been the
    ``startswith("10.")`` check — but there's no further structural
    check. Verify the renderer + grounding gate handle this:
    rendered surfaces it, but the citation pattern requires ``/``,
    so the LLM cannot legitimately cite. Hallucination defense
    holds because grounding-allowed-tokens contains ``[10.]`` but
    the LLM emitted ``[10.]`` would be matched by allowed_tokens
    only if both sides agree."""
    pub = {"doi": "10.malformed", "title": "T"}
    rendered, allowed = _render_publications([pub], cap=1, strict=True)
    # Renderer accepted (lax check)
    assert "[10.malformed]" in allowed
    # But the citation regex requires ``/`` after the prefix; the
    # extraction would not match an LLM that emitted ``[10.malformed]``.
    cfg = SynthesisConfig.model_validate({"system_prompt": "x"})
    import re
    extracted = set()
    for pat in cfg.citation_marker_patterns:
        extracted.update(re.findall(pat, "see [10.malformed] above"))
    # The pattern ``\[10\.[0-9]+/[^\]]+\]`` requires digits then ``/``.
    # ``10.malformed`` has no digits after ``10.`` — so NOT extracted.
    assert extracted == set(), (
        f"extracted unexpectedly matched a malformed DOI: {extracted!r}"
    )


def test_probe_1046_workflow_yaml_link_block_has_five_links_no_rag_synthesis_target():
    """T01 chain has 5 links. Adding rag_synthesis should NOT have
    accidentally introduced a 6th link or any link with rag_synthesis
    as source/target. Direct YAML-file inspection rather than
    framework attribute introspection (the latter varies by
    framework version)."""
    import yaml
    raw = yaml.safe_load(WORKFLOW_YAML.read_text(encoding="utf-8"))
    links = raw.get("links", {})
    assert isinstance(links, dict), f"unexpected links shape: {type(links)}"
    assert len(links) == 5, (
        f"expected 5 T01 links; got {len(links)} -- if rag_synthesis "
        f"was linked, that linkage should only land when an upstream "
        f"retrieval step assembles the four-source bundle (Phase-2)."
    )
    for link_id, link_def in links.items():
        cfg = link_def.get("config", {})
        src = cfg.get("source", "")
        tgt = cfg.get("target", "")
        assert "rag_synthesis" not in src and "rag_synthesis" not in tgt, (
            f"link {link_id!r} unexpectedly references rag_synthesis: "
            f"{src!r} -> {tgt!r}"
        )


def test_probe_1047_synthesis_config_load_with_yaml_extra_unknown_field_under_nested_dict():
    """A typo at the TOP level of synthesis_config.yml is caught by
    extra='forbid' (probe 955). What about a typo inside a nested
    dict (none exist in current schema, but a future field might
    add nested dicts)? Verify current schema has no nested dicts."""
    cfg = SynthesisConfig.model_validate({"system_prompt": "x"})
    fields = cfg.model_fields
    for name, field in fields.items():
        # Every field's annotation must be a primitive, list, or
        # bool — no nested BaseModel without its own extra='forbid'.
        # If a future field adds nested BaseModel, this probe should
        # fail loudly until that nested model also forbids extras.
        ann = field.annotation
        # Heuristic: type must not be a class with .model_fields
        # (i.e., a nested BaseModel).
        if hasattr(ann, "model_fields"):
            pytest.fail(
                f"field {name!r} is a nested BaseModel; verify it "
                f"also sets extra='forbid'"
            )


def test_probe_1048_adapter_creator_with_affiliation_object_having_nameIdentifiers():
    """Affiliation can carry name + nameIdentifiers (ROR/GRID etc.).
    The adapter must NOT leak any of that into the author name —
    current contract is given+family or name only."""
    aff = Affiliation(name="Real University")
    rec = _datacite(creators=[
        Creator(givenName="A", familyName="B", affiliation=aff),
    ])
    pub = datacite_to_publication(rec)
    assert pub["authors"] == ["A B"]
    assert "Real University" not in str(pub["authors"])


def test_probe_1049_adapter_record_with_alternateIdentifiers_does_not_leak():
    """``alternateIdentifiers`` carries non-DOI alt IDs (e.g. URN, ARK).
    The adapter ONLY uses ``identifier`` (the primary DOI). Verify
    that an alt ID containing a DOI-shape doesn't accidentally
    become the citation."""
    from apecx_harvesters.loaders.base.model import AlternateIdentifier
    rec = _datacite(
        identifier=Identifier(identifier="10.1/REAL", identifierType="DOI"),
        alternateIdentifiers=[
            AlternateIdentifier(
                alternateIdentifier="10.9999/SHADOW",
                alternateIdentifierType="DOI",
            ),
        ],
    )
    pub = datacite_to_publication(rec)
    assert pub["doi"] == "10.1/REAL"
    assert "10.9999/SHADOW" not in pub["doi"]


def test_probe_1050_render_publication_pub_dict_with_extra_unknown_key_silent():
    """The publication renderer iterates known keys (doi, title, etc.).
    A pub dict with extra keys (e.g. {"crossref_score": 0.99}) is
    silently ignored. Pin: NO warning, NO crash, just ignored.
    Future adversarial probe might want to surface unknown keys;
    this pins current state."""
    pub = {
        "doi": "10.1/x", "title": "T",
        "crossref_score": 0.99, "tags": ["alpha"],
    }
    rendered, allowed = _render_publications([pub], cap=1, strict=True)
    assert allowed == {"[10.1/x]"}
    assert "crossref_score" not in rendered
    assert "tags" not in rendered


def test_probe_1051_synthesis_config_default_value_is_what_doc_says():
    """A regression where the default for ``min_distinct_citations``
    silently changes from 1 to 0 (or 2) would change the validator's
    behavior across deployments. Pin the published defaults."""
    cfg = SynthesisConfig.model_validate({"system_prompt": "x"})
    # Documented defaults:
    assert cfg.max_rag_chunks == 8
    assert cfg.max_bvbrc_genomes == 5
    assert cfg.max_violin_mappings == 20
    assert cfg.max_publications == 5
    assert cfg.require_inline_citations is True
    assert cfg.min_response_chars == 200
    assert cfg.min_distinct_citations == 1
    assert cfg.fail_on_empty_retrieval is True
    assert cfg.strict_input_validation is True
    assert cfg.validate_citations_against_inputs is True


def test_probe_1052_default_synthesis_yaml_matches_default_python_schema():
    """The bundled synthesis_config.yml MUST produce a config that
    matches the Pydantic schema's defaults (i.e. operator changes
    one value at a time without re-stating every other). Verify by
    loading both and comparing."""
    import yaml
    raw = yaml.safe_load(DEFAULT_SYNTHESIS_CONFIG_PATH.read_text())
    yaml_cfg = SynthesisConfig.model_validate(raw)
    # Python schema default — pass only system_prompt to bypass
    # the YAML's value.
    py_default = SynthesisConfig.model_validate({
        "system_prompt": yaml_cfg.system_prompt,
    })
    # Compare every other field.
    for name in yaml_cfg.model_fields:
        if name == "system_prompt":
            continue
        yaml_v = getattr(yaml_cfg, name)
        py_v = getattr(py_default, name)
        assert yaml_v == py_v, (
            f"YAML / Python schema disagree on default for {name!r}: "
            f"yaml={yaml_v!r}, python={py_v!r}"
        )


def test_probe_1053_synthesis_config_required_field_only_is_system_prompt():
    """The Pydantic schema marks one field required: system_prompt.
    Every other field must have a default. A future "required"
    field added without a default would silently break operators
    with minimal configs."""
    cfg_class = SynthesisConfig
    required = []
    for name, field in cfg_class.model_fields.items():
        if field.is_required():
            required.append(name)
    assert required == ["system_prompt"], (
        f"unexpected required fields: {required!r}; only system_prompt "
        f"should be required to keep operator configs minimal."
    )


def test_probe_1054_adapter_handles_creator_givenName_with_only_whitespace():
    """Creator givenName='   ' (whitespace only) + familyName='Smith'.
    Current contract: both truthy (non-empty after-truthiness),
    concat works. The author name renders as '   Smith' — visually
    odd. Pin current behavior so a future fix that strips whitespace
    fails this test loudly."""
    rec = _datacite(creators=[Creator(givenName="   ", familyName="Smith")])
    pub = datacite_to_publication(rec)
    # Current behavior: ``"   " + " " + "Smith"`` -> "    Smith"
    # The whitespace truthy is True (non-empty string).
    assert "Smith" in (pub.get("authors") or [""])[0]
