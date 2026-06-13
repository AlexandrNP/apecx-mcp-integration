"""Offline unit tests for the E3-3 residue-annotation bridge + cross-check.

These pin the numbering-bridge LOGIC (the +809 SIFTS offset, membership tests, epitope
location, IEDB cs.{} syntax, parser shapes) without network. Each behavior verified here
by a fixture has a matching REAL-API test in
``tests/integration/test_functional_residue_annotation_real.py`` (CC-3 parity).
"""

from __future__ import annotations

from apecx_integration.agents.functional import sifts_client
from apecx_integration.agents.functional.iedb_client import IedbClient, containment_param
from apecx_integration.agents.functional.residue_annotation import (
    cross_check_residues,
    feature_covers,
    locate_epitope_spans,
)
from apecx_integration.agents.functional.sifts_client import SiftsClient
from apecx_integration.agents.functional.uniprot_client import UniProtClient

# Real 2XFB SIFTS payload shape (chains A and B map to the SAME accession Q1H8W5 at
# DIFFERENT offsets — the per-chain bridge must distinguish them).
_SIFTS_2XFB = {
    "2xfb": {
        "UniProt": {
            "Q1H8W5": {
                "mappings": [
                    {
                        "chain_id": "A",
                        "start": {"residue_number": 1, "author_residue_number": 1},
                        "end": {"residue_number": 391, "author_residue_number": 391},
                        "unp_start": 810,
                        "unp_end": 1200,
                    },
                    {
                        "chain_id": "B",
                        "start": {"residue_number": 1, "author_residue_number": 72},
                        "end": {"residue_number": 334, "author_residue_number": 405},
                        "unp_start": 333,
                        "unp_end": 666,
                    },
                ]
            }
        }
    }
}


def test_sifts_parse_extracts_author_frame_segments():
    parsed = SiftsClient._parse(_SIFTS_2XFB, "2xfb")
    assert "Q1H8W5" in parsed
    segs = parsed["Q1H8W5"]
    assert {s["chain_id"] for s in segs} == {"A", "B"}
    a = next(s for s in segs if s["chain_id"] == "A")
    assert (a["author_start"], a["author_end"], a["unp_start"]) == (1, 391, 810)


def test_bridge_809_offset_chain_a():
    """Load-bearing fixture: 2XFB chain A author resi 1 → UniProt Q1H8W5 810 (+809).

    A wrong offset (e.g. using residue_number instead of author, or RCSB label numbering)
    fails here.
    """
    mappings = SiftsClient._parse(_SIFTS_2XFB, "2xfb")
    segs_a = sifts_client.chain_segments(mappings, "A")
    assert segs_a and segs_a[0]["offset"] == 809
    assert sifts_client.bridge_residue(segs_a, 1) == ("Q1H8W5", 810)
    assert sifts_client.bridge_residue(segs_a, 141) == ("Q1H8W5", 950)  # the glyco residue
    assert sifts_client.bridge_residue(segs_a, 391) == ("Q1H8W5", 1200)


def test_bridge_per_chain_offset_differs():
    """Chain B has a different offset (+261); the bridge must not bleed chain A's offset."""
    mappings = SiftsClient._parse(_SIFTS_2XFB, "2xfb")
    segs_b = sifts_client.chain_segments(mappings, "B")
    assert segs_b[0]["offset"] == 261
    assert sifts_client.bridge_residue(segs_b, 72) == ("Q1H8W5", 333)


def test_bridge_out_of_range_returns_none():
    mappings = SiftsClient._parse(_SIFTS_2XFB, "2xfb")
    segs_a = sifts_client.chain_segments(mappings, "A")
    assert sifts_client.bridge_residue(segs_a, 9999) is None


def test_uniprot_parse_features_and_release_header():
    body = {
        "entryType": "UniProtKB unreviewed (TrEMBL)",
        "sequence": {"value": "MKT" + "X" * 950},
        "features": [
            {
                "type": "Glycosylation",
                "location": {"start": {"value": 950}, "end": {"value": 950}},
                "description": "N-linked (GlcNAc...) asparagine",
            },
            {  # malformed (no end) — must be skipped, not crash
                "type": "Disulfide bond",
                "location": {"start": {"value": 5}},
                "description": "",
            },
        ],
    }
    entry = UniProtClient._parse(body, "Q1H8W5", {"x-uniprot-release": "2026_02"})
    assert entry["release"] == "2026_02"
    assert entry["accession"] == "Q1H8W5"
    assert len(entry["features"]) == 1
    assert entry["features"][0]["type"] == "Glycosylation"
    assert entry["features"][0]["start"] == 950


def test_iedb_containment_syntax_is_pinned():
    """The PostgREST array-containment form is load-bearing — pin it so a refactor to the
    singular ``eq.`` form (which errors on the array column) fails LOUD, not silently []."""
    assert containment_param("Q1H8W5") == "cs.{UNIPROT:Q1H8W5}"
    assert containment_param("P0DTC2") == "cs.{UNIPROT:P0DTC2}"


def test_iedb_parse_drops_nonlinear_and_empty():
    rows = [
        {"linear_sequence": "VVFLHVTYV", "structure_type": "Linear peptide", "pdb_ids": None},
        {"linear_sequence": None, "structure_type": "Discontinuous"},
        {"structure_type": "Linear peptide"},
        "garbage",
    ]
    out = IedbClient._parse(rows)
    assert len(out) == 1
    assert out[0]["linear_sequence"] == "VVFLHVTYV"


def test_locate_epitope_spans_one_based():
    seq = "AAAVVFLHVTYVAAA"  # epitope at index 3 (0-based) → 1-based start 4
    spans = locate_epitope_spans(seq, "VVFLHVTYV")
    assert spans == [(4, 12)]
    assert locate_epitope_spans(seq, "ZZZ") == []


def test_cross_check_surfaces_glyco_coincidence():
    """A candidate author residue overlapping a real glyco span surfaces a coincidence."""
    ctx = {
        "available": True,
        "chain": "A",
        "accessions": ["Q1H8W5"],
        "segments": [
            {
                "accession": "Q1H8W5",
                "author_start": 1,
                "author_end": 391,
                "unp_start": 810,
                "unp_end": 1200,
                "offset": 809,
            }
        ],
        "features_by_acc": {
            "Q1H8W5": [
                {"type": "Glycosylation", "start": 950, "end": 950, "description": "N-linked"}
            ]
        },
        "iedb_spans_by_acc": {"Q1H8W5": []},
        "n_uniprot_features": 1,
        "n_iedb_epitope_spans": 0,
    }
    out = cross_check_residues([141, 50], ctx)  # 141 → unp 950 (glyco); 50 → unp 859 (none)
    assert any(c["residue"] == 141 and c["type"] == "Glycosylation" for c in out["coincidences"])
    # CC-1: a per-residue named line for EVERY candidate (coincidence OR explicit absence).
    assert len(out["residue_findings"]) == 2
    assert any(
        "no functional/immunological feature at residue 50" in f for f in out["residue_findings"]
    )


def test_disulfide_is_endpoints_not_a_span():
    """A disulfide "858..923" joins Cys858–Cys923 ONLY; residues between must NOT coincide
    (real bug caught on Q1H8W5: range membership falsely covered 859-922)."""
    disulfide = {"type": "Disulfide bond", "start": 858, "end": 923, "description": ""}
    assert feature_covers(disulfide, 858) is True
    assert feature_covers(disulfide, 923) is True
    assert feature_covers(disulfide, 890) is False  # between the two cysteines — NOT covered
    domain = {"type": "Domain", "start": 113, "end": 261, "description": "Peptidase S3"}
    assert feature_covers(domain, 200) is True  # ranges still span inclusively


def test_cross_check_disulfide_between_residue_is_named_absence():
    ctx = {
        "available": True,
        "chain": "A",
        "accessions": ["Q1H8W5"],
        "segments": [
            {
                "accession": "Q1H8W5",
                "author_start": 1,
                "author_end": 391,
                "unp_start": 810,
                "unp_end": 1200,
                "offset": 809,
            }
        ],
        "features_by_acc": {
            "Q1H8W5": [{"type": "Disulfide bond", "start": 858, "end": 923, "description": ""}]
        },
        "iedb_spans_by_acc": {"Q1H8W5": []},
        "n_uniprot_features": 1,
        "n_iedb_epitope_spans": 0,
    }
    # author 81 → unp 890 (between the two cysteines) → must be a NAMED absence, not a hit.
    out = cross_check_residues([81], ctx)
    assert out["coincidences"] == []
    assert "no functional/immunological feature at residue 81" in out["residue_findings"][0]
    # author 49 → unp 858 (a cysteine endpoint) → a real disulfide coincidence.
    out2 = cross_check_residues([49], ctx)
    assert any(c["residue"] == 49 and c["type"] == "Disulfide bond" for c in out2["coincidences"])


def test_cross_check_no_candidates_still_named():
    """available=True but zero candidates → a non-empty named finding (never silent empty)."""
    ctx = {
        "available": True,
        "chain": "A",
        "accessions": ["Q1H8W5"],
        "segments": [],
        "features_by_acc": {"Q1H8W5": []},
        "iedb_spans_by_acc": {"Q1H8W5": []},
        "n_uniprot_features": 0,
        "n_iedb_epitope_spans": 0,
    }
    out = cross_check_residues([], ctx)
    assert out["coincidences"] == []
    assert len(out["residue_findings"]) == 1 and out["residue_findings"][0]
