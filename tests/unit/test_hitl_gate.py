"""Unit tests for the shared HITL gate.

The gate is the structural guarantee that no MCP database tool
silently mis-attributes an ambiguous user term to one of its
multiple canonical IRIs. Two behaviors pinned here:

1. The ``detect_ambiguity`` synonym-dictionary helper detects
   multi-IRI surface forms via the ``ambiguous_surface_forms``
   table (authoritative source) with a ``lookup_any_type``
   fallback (catches conflicts the build pass missed).

2. ``resolve_with_hitl_gate`` returns one of three shapes:
   ``bypass`` (empty term), ``paused_awaiting_disambiguation``
   (ambiguous), or ``resolved`` (single canonical match).

Both are exercised against in-memory fake indexes (no production
dictionary required), so the tests run in milliseconds and survive
dictionary rebuilds.
"""

from __future__ import annotations

from dataclasses import dataclass

from apecx_integration.mcp_surface.tools._hitl_gate import (
    resolve_with_hitl_gate,
)
from apecx_integration.synonym_dictionary import lookup as lookup_mod
from apecx_integration.synonym_dictionary.enums import (
    EntityType,
    OntologyName,
    ResolutionStatus,
)
from apecx_integration.synonym_dictionary.lookup import (
    LookupResult,
    detect_ambiguity,
)

# ─────────────────────────────────────────────────────────────────────────
# Test fakes: small stand-in for the DictionaryIndex
# ─────────────────────────────────────────────────────────────────────────


@dataclass
class _FakeEntry:
    """Subset of DictionaryEntry the gate touches."""

    entity_type: str
    canonical_iri: str
    canonical_label: str
    ontology: OntologyName = OntologyName.NCBITAXON
    ontology_version: str = "test"
    source_records: tuple = ()
    confidence: float = 1.0
    synonyms: tuple = ()


class _FakeIndex:
    """In-memory stand-in for DictionaryIndex used by the gate."""

    def __init__(
        self,
        *,
        ambiguous_rows: list[dict[str, str]] | None = None,
        any_type_matches: list[_FakeEntry] | None = None,
        iri_lookups: dict[str, _FakeEntry] | None = None,
    ) -> None:
        self._amb_rows = ambiguous_rows or []
        self._any_type = any_type_matches or []
        self._iri_lookups = iri_lookups or {}

    def lookup_ambiguous_surface_forms(self, *, surface_form: str, limit: int = 50):
        return self._amb_rows

    def lookup_any_type(self, surface_form: str) -> list[_FakeEntry]:
        return self._any_type

    def lookup_by_iri(self, iri: str) -> _FakeEntry | None:
        return self._iri_lookups.get(iri)


def _patch_index(monkeypatch, index: _FakeIndex | None):
    """Patch get_dictionary_index() to return our fake."""

    def fake_get():
        return (index, None)

    monkeypatch.setattr(
        lookup_mod,
        "get_dictionary_index",
        fake_get,
    )


def _patch_lookup_entity(monkeypatch, result: LookupResult):
    """Patch lookup_entity to return a deterministic LookupResult."""
    monkeypatch.setattr(
        lookup_mod,
        "lookup_entity",
        lambda surface_form, *, entity_type=None: result,
    )


def _fast_result(
    term: str = "RSV",
    iri: str = "http://x/A",
    label: str = "Candidate A",
) -> LookupResult:
    return LookupResult(
        surface_form=term,
        path="fast",
        canonical_iri=iri,
        canonical_label=label,
        canonical_ontology="ncbitaxon",
        confidence=1.0,
        resolution_status=ResolutionStatus.ID_ANCHORED,
        synonyms=(),
        evidence="",
    )


# ─────────────────────────────────────────────────────────────────────────
# detect_ambiguity
# ─────────────────────────────────────────────────────────────────────────


def test_detect_ambiguity_iri_input_is_unambiguous(monkeypatch):
    """IRI input bypasses the ambiguity check entirely."""
    _patch_lookup_entity(monkeypatch, _fast_result(term="http://x/A"))
    _patch_index(
        monkeypatch,
        _FakeIndex(
            ambiguous_rows=[
                {"winning_canonical_iri": "http://x/A", "alternative_canonical_iri": "http://x/B"},
            ]
        ),
    )
    primary, candidates = detect_ambiguity("http://x/A")
    assert candidates == []
    assert primary.path == "fast"


def test_detect_ambiguity_picks_up_six_way_rsv_from_ambiguous_table(monkeypatch):
    """The exact production shape: RSV maps to 6 distinct IRIs in the
    ambiguous_surface_forms table; gate returns all 6 candidates."""
    iri_lookups = {
        f"http://x/RSV_{i}": _FakeEntry(
            entity_type="pathogen",
            canonical_iri=f"http://x/RSV_{i}",
            canonical_label=f"RSV variant {i}",
        )
        for i in range(1, 7)
    }
    # Ambiguous table emits 5 rows; each row pairs winning + alternative.
    # 6 distinct IRIs total via (winning_1, alt_1=2), (winning_1, alt_3) ...
    ambiguous_rows = [
        {
            "winning_canonical_iri": "http://x/RSV_1",
            "alternative_canonical_iri": f"http://x/RSV_{i}",
        }
        for i in range(2, 7)
    ]
    _patch_lookup_entity(monkeypatch, _fast_result(term="RSV", iri="http://x/RSV_1"))
    _patch_index(
        monkeypatch,
        _FakeIndex(
            ambiguous_rows=ambiguous_rows,
            iri_lookups=iri_lookups,
        ),
    )

    primary, candidates = detect_ambiguity("RSV")
    assert len(candidates) == 6
    iris = {c["canonical_iri"] for c in candidates}
    assert iris == set(iri_lookups.keys())


def test_detect_ambiguity_lookup_any_type_fallback(monkeypatch):
    """When the ambiguous_surface_forms table is empty BUT lookup_any_type
    returns multiple distinct IRIs, the gate still fires."""
    matches = [
        _FakeEntry("pathogen", "http://x/A", "Entity A"),
        _FakeEntry("pathogen", "http://x/B", "Entity B"),
    ]
    iri_lookups = {e.canonical_iri: e for e in matches}
    _patch_lookup_entity(monkeypatch, _fast_result(term="X", iri="http://x/A"))
    _patch_index(
        monkeypatch,
        _FakeIndex(
            ambiguous_rows=[],  # build pass missed it
            any_type_matches=matches,
            iri_lookups=iri_lookups,
        ),
    )
    _primary, candidates = detect_ambiguity("X")
    assert len(candidates) == 2


def test_detect_ambiguity_single_match_is_unambiguous(monkeypatch):
    _patch_lookup_entity(monkeypatch, _fast_result())
    _patch_index(
        monkeypatch,
        _FakeIndex(
            ambiguous_rows=[],
            any_type_matches=[_FakeEntry("pathogen", "http://x/A", "Solo")],
        ),
    )
    _primary, candidates = detect_ambiguity("X")
    assert candidates == []


def test_detect_ambiguity_no_index_degrades_gracefully(monkeypatch):
    """When the dict isn't loaded, the gate returns the primary result
    with empty candidates — the caller proceeds optimistically."""
    _patch_lookup_entity(monkeypatch, _fast_result())
    _patch_index(monkeypatch, None)
    primary, candidates = detect_ambiguity("X")
    assert candidates == []
    assert primary.path == "fast"


# ─────────────────────────────────────────────────────────────────────────
# resolve_with_hitl_gate
# ─────────────────────────────────────────────────────────────────────────


def test_gate_bypass_for_empty_term(monkeypatch):
    """Empty term → bypass; no resolution attempted (tool runs without filter)."""
    out = resolve_with_hitl_gate(
        term="",
        entity_type=None,
        param_name="search_term",
        tool_name="query_pathogens",
    )
    assert out["status"] == "bypass"


def test_gate_bypass_for_whitespace_term(monkeypatch):
    out = resolve_with_hitl_gate(
        term="   ",
        entity_type=None,
        param_name="search_term",
        tool_name="query_pathogens",
    )
    assert out["status"] == "bypass"


def test_gate_resolved_for_unambiguous(monkeypatch):
    """Unambiguous term → resolved with lookup_result + ncbi_taxonomy_id."""
    _patch_lookup_entity(
        monkeypatch,
        _fast_result(
            iri="http://purl.obolibrary.org/obo/NCBITaxon_37124",
        ),
    )
    _patch_index(monkeypatch, _FakeIndex())
    out = resolve_with_hitl_gate(
        term="CHIKV",
        entity_type=EntityType.PATHOGEN,
        param_name="search_term",
        tool_name="query_bvbrc_genomes",
    )
    assert out["status"] == "resolved"
    assert out["ncbi_taxonomy_id"] == 37124
    assert out["lookup_result"].canonical_iri == "http://purl.obolibrary.org/obo/NCBITaxon_37124"
    assert out["resolution_meta"]["canonical_label"] == "Candidate A"


def test_gate_paused_for_ambiguous(monkeypatch):
    """Ambiguous term → paused envelope identical in shape to harmonized_search's.
    Critical assertions:
      - no lookup_result is returned (the gate did NOT pick one)
      - next_action.options carries all candidate IRIs
      - markdown surfaces all candidates for the user-facing LLM to display
    """
    iri_lookups = {
        "http://x/A": _FakeEntry("pathogen", "http://x/A", "Cand A"),
        "http://x/B": _FakeEntry("pathogen", "http://x/B", "Cand B"),
        "http://x/C": _FakeEntry("pathogen", "http://x/C", "Cand C"),
    }
    _patch_lookup_entity(monkeypatch, _fast_result(term="RSV", iri="http://x/A"))
    _patch_index(
        monkeypatch,
        _FakeIndex(
            ambiguous_rows=[
                {"winning_canonical_iri": "http://x/A", "alternative_canonical_iri": "http://x/B"},
                {"winning_canonical_iri": "http://x/A", "alternative_canonical_iri": "http://x/C"},
            ],
            iri_lookups=iri_lookups,
        ),
    )

    out = resolve_with_hitl_gate(
        term="RSV",
        entity_type=EntityType.PATHOGEN,
        param_name="search_term",
        tool_name="query_bvbrc_genomes",
    )
    assert out["status"] == "paused_awaiting_disambiguation"
    assert "lookup_result" not in out
    assert len(out["next_action"]["options"]) == 3
    assert set(out["next_action"]["options"]) == {
        "http://x/A",
        "http://x/B",
        "http://x/C",
    }
    assert out["next_action"]["tool"] == "query_bvbrc_genomes"
    assert out["next_action"]["param_name"] == "search_term"

    # Markdown carries all candidates for the user-facing LLM to render.
    assert "Cand A" in out["markdown"]
    assert "Cand B" in out["markdown"]
    assert "Cand C" in out["markdown"]


def test_gate_iri_input_short_circuits_to_resolved(monkeypatch):
    """An IRI input (round-2 after disambiguation) is unambiguous by
    construction — the gate passes through to resolved without firing."""
    _patch_lookup_entity(
        monkeypatch,
        _fast_result(
            term="http://purl.obolibrary.org/obo/NCBITaxon_11250",
            iri="http://purl.obolibrary.org/obo/NCBITaxon_11250",
            label="human respiratory syncytial virus",
        ),
    )
    # Even if the ambiguous table has rows, IRI input skips the check.
    _patch_index(
        monkeypatch,
        _FakeIndex(
            ambiguous_rows=[
                {"winning_canonical_iri": "http://x/A", "alternative_canonical_iri": "http://x/B"},
            ],
        ),
    )
    out = resolve_with_hitl_gate(
        term="http://purl.obolibrary.org/obo/NCBITaxon_11250",
        entity_type=EntityType.PATHOGEN,
        param_name="search_term",
        tool_name="query_bvbrc_genomes",
    )
    assert out["status"] == "resolved"
    assert out["ncbi_taxonomy_id"] == 11250


def test_gate_paused_envelope_shape_matches_harmonized_search_contract():
    """The paused-envelope shape MUST match what a user-facing LLM
    receives from harmonized_search. Pins the contract that's load-bearing
    for cross-tool conversation consistency."""
    # Required top-level keys
    expected_keys = {
        "status",
        "markdown",
        "next_action",
        "candidates",
        "tool",
        "term",
    }
    # Required next_action sub-keys
    expected_next_action_keys = {"kind", "tool", "param_name", "options"}
    # We synthesize a minimal paused envelope via the helper directly
    # rather than going through the gate (no monkeypatching needed).
    from apecx_integration.mcp_surface.tools._hitl_gate import (
        _build_paused_envelope,
    )

    env = _build_paused_envelope(
        term="RSV",
        candidates=[
            {
                "canonical_iri": "http://x/A",
                "canonical_label": "A",
                "canonical_ontology": "n",
                "confidence": 1.0,
            },
            {
                "canonical_iri": "http://x/B",
                "canonical_label": "B",
                "canonical_ontology": "n",
                "confidence": 1.0,
            },
        ],
        param_name="search_term",
        tool_name="query_pathogens",
    )
    assert set(env.keys()) == expected_keys
    assert set(env["next_action"].keys()) == expected_next_action_keys
    assert env["next_action"]["kind"] == "re-invoke_with_chosen_iri"
    assert env["status"] == "paused_awaiting_disambiguation"
