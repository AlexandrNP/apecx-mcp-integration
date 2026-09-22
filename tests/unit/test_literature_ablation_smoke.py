"""Tests for the organism-filter ablation harness.

Two layers, per the workspace mocks policy:

  1. OFFLINE unit (always runs, NO LLM, NO network): a hand-built 4-record corpus
     + a FAKE reader that cites the top-1 record it is given (by input order).
     Record order is arranged so the UNFILTERED reader's top-1 is a dengue record
     (precision 0.0) while the FILTERED reader's top-1 is a CHIKV record
     (precision 1.0). Asserts mean_filtered_precision > mean_unfiltered_precision,
     and pins ``organism_precision`` on two hand-checked cases.

  2. Ollama-GATED integration (real sentence-transformers retrieval + real LLM):
     two cases (CHIKV, dengue) over a stamped fixture corpus, via
     ``default_reader``. Asserts mean_filtered_precision >= mean_unfiltered_precision
     (the filter must not hurt). The offline unit is authoritative; the integration
     result is reported honestly and never faked.

Run:
    PYTHONPATH=src .venv/bin/python -m pytest \
        tests/unit/test_literature_ablation_smoke.py -q
"""

from __future__ import annotations

import os

import httpx
import pytest

from apecx_integration.agents.literature.ablation import (
    default_reader,
    merge_failure_catalogs,
    mine_rag_failures,
    organism_precision,
    retrieval_reader,
    run_ablation,
)
from apecx_integration.agents.literature.gazetteer import build_gazetteer
from apecx_integration.agents.literature.stamped_corpus import stamp_abstract

_CHIKV_IRI = "http://purl.obolibrary.org/obo/NCBITaxon_37124"
_DENGUE_IRI = "http://purl.obolibrary.org/obo/NCBITaxon_12637"


# --------------------------------------------------------------------------- #
# 1. OFFLINE unit — no LLM, no network, always runs.
# --------------------------------------------------------------------------- #


def _fake_top1_reader(question: str, records: list[dict], *, k: int = 5) -> dict:
    """Cite the top-1 record by input order (no retrieval, no LLM)."""
    return {"citations": [records[0]["pmid"]] if records else []}


def test_organism_precision_hand_checked():
    """Exact 1.0 / 0.0 on the two hand-checked citation sets."""
    corpus_by_pmid = {
        "c1": {"pmid": "c1", "iris": [_CHIKV_IRI]},
        "d1": {"pmid": "d1", "iris": [_DENGUE_IRI]},
    }
    assert organism_precision(["c1"], corpus_by_pmid, _CHIKV_IRI) == 1.0
    assert organism_precision(["d1"], corpus_by_pmid, _CHIKV_IRI) == 0.0
    assert organism_precision([], corpus_by_pmid, _CHIKV_IRI) == 0.0


def test_filter_lifts_organism_precision_offline():
    """Filter isolates the on-organism records, so the fake top-1 reader cites a
    CHIKV PMID when filtered but a dengue PMID over the whole mixed corpus."""
    # Order matters: a dengue record is FIRST, so the unfiltered top-1 is off-organism.
    corpus = [
        {"pmid": "d1", "iris": [_DENGUE_IRI]},
        {"pmid": "c1", "iris": [_CHIKV_IRI]},
        {"pmid": "d2", "iris": [_DENGUE_IRI]},
        {"pmid": "c2", "iris": [_CHIKV_IRI]},
    ]
    cases = [{"organism": "CHIKV", "question": "x", "correct_iri": _CHIKV_IRI}]

    result = run_ablation(cases, corpus, _fake_top1_reader)

    assert result["mean_filtered_precision"] == 1.0
    assert result["mean_unfiltered_precision"] == 0.0
    assert result["mean_filtered_precision"] > result["mean_unfiltered_precision"]
    assert result["n_cases"] == 1
    case = result["per_case"][0]
    assert case["filtered_citations"] == ["c1"]
    assert case["unfiltered_citations"] == ["d1"]


def test_mine_rag_failures_offline():
    """From per-case citations, surface the concrete pure-RAG false positive and
    false negative, each labelled with the paper it actually concerns."""
    corpus_by_pmid = {
        "c1": {"pmid": "c1", "iris": [_CHIKV_IRI], "title": "CHIKV antibody"},
        "c2": {"pmid": "c2", "iris": [_CHIKV_IRI], "title": "CHIKV vaccine"},
        "d1": {"pmid": "d1", "iris": [_DENGUE_IRI], "title": "Dengue antibody"},
    }
    iri_to_name = {_CHIKV_IRI: "Chikungunya virus", _DENGUE_IRI: "Dengue virus"}
    # For a CHIKV question: filtered cites two CHIKV papers; pure RAG cites one
    # CHIKV paper (c1) plus an off-organism dengue paper (d1), and misses c2.
    per_case = [
        {
            "organism": "Chikungunya virus",
            "question": "What antibodies neutralize Chikungunya virus?",
            "correct_iri": _CHIKV_IRI,
            "filtered_citations": ["c1", "c2"],
            "unfiltered_citations": ["c1", "d1"],
        }
    ]

    out = mine_rag_failures(per_case, corpus_by_pmid, iri_to_name)

    assert len(out["false_positives"]) == 1
    fp = out["false_positives"][0]
    assert fp["cited_pmid"] == "d1"
    assert fp["actual_organisms"] == ["Dengue virus"]
    assert fp["title"] == "Dengue antibody"

    assert len(out["false_negatives"]) == 1
    fn = out["false_negatives"][0]
    assert fn["missed_pmid"] == "c2"
    assert fn["title"] == "CHIKV vaccine"


def test_merge_failure_catalogs_accumulates_and_dedupes():
    """A second run adds genuinely new failures but does not double-count a repeat
    of the same (organism, pmid) example — the catalog grows, not churns."""
    existing = {
        "false_positives": [
            {"organism": "Zika virus", "cited_pmid": "d1", "actual_organisms": ["Dengue virus"]},
        ],
        "false_negatives": [
            {"organism": "Ebola virus", "missed_pmid": "e9", "title": "missed"},
        ],
    }
    new = {
        "false_positives": [
            # exact repeat — must NOT be added again
            {"organism": "Zika virus", "cited_pmid": "d1", "actual_organisms": ["Dengue virus"]},
            # genuinely new
            {"organism": "Ebola virus", "cited_pmid": "b7", "actual_organisms": ["Bundibugyo"]},
        ],
        "false_negatives": [
            {"organism": "Ebola virus", "missed_pmid": "e9", "title": "missed"},  # repeat
            {"organism": "Yellow fever virus", "missed_pmid": "y3", "title": "new"},  # new
        ],
    }

    merged = merge_failure_catalogs(existing, new)

    assert len(merged["false_positives"]) == 2  # d1 kept once + b7 added
    assert {fp["cited_pmid"] for fp in merged["false_positives"]} == {"d1", "b7"}
    assert len(merged["false_negatives"]) == 2  # e9 kept once + y3 added
    assert {fn["missed_pmid"] for fn in merged["false_negatives"]} == {"e9", "y3"}
    # empty existing (first run) is a no-op union
    assert merge_failure_catalogs({}, new)["false_positives"] == new["false_positives"]


def test_retrieval_reader_caches_subindex_by_pmid_set(monkeypatch):
    """The reader builds one sub-index per DISTINCT record set and reuses it, so an
    ablation that calls it once per case does not re-embed the same pool each time."""
    import sentence_transformers

    import apecx_integration.agents.literature.stamped_corpus as sc

    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", lambda *a, **k: object())
    calls = {"n": 0}

    class _FakeSub:
        def __init__(self, records):
            self._records = list(records)

        def search(self, query, k=5):
            return self._records[:k]

    def _fake_build(records, **kwargs):
        calls["n"] += 1
        return _FakeSub(records)

    monkeypatch.setattr(sc, "build_faiss_subindex", _fake_build)

    reader = retrieval_reader()
    pool = [{"pmid": "a"}, {"pmid": "b"}, {"pmid": "c"}]
    reader("q1", pool)
    reader("q2", pool)  # same PMID set -> reuse, no new build
    assert calls["n"] == 1
    reader("q3", pool[:2])  # different PMID set -> one new build
    assert calls["n"] == 2


# --------------------------------------------------------------------------- #
# 2. Ollama-GATED integration — real retrieval + real LLM.
# --------------------------------------------------------------------------- #

_OLLAMA_URL = os.environ.get("APECX_LLM_BASE_URL", "http://localhost:11434/v1")
_OLLAMA_ROOT = _OLLAMA_URL[:-3].rstrip("/") if _OLLAMA_URL.endswith("/v1") else _OLLAMA_URL
_LLM_MODEL = os.environ.get("APECX_LLM_MODEL", "mistral-nemo:latest")


def _ollama_reachable() -> bool:
    """True iff the Ollama endpoint is reachable AND the target model is pulled."""
    try:
        r = httpx.get(f"{_OLLAMA_ROOT}/api/tags", timeout=3.0)
        r.raise_for_status()
        names = {m["name"] for m in r.json().get("models", [])}
        stem = _LLM_MODEL.split(":", 1)[0]
        return any(n == _LLM_MODEL or n.split(":", 1)[0] == stem for n in names)
    except Exception:
        return False


# Realistic CHIKV + dengue abstracts with distinct PMIDs (real domain facts, not
# synthetic noise). Each is stamped with its organism IRI via the gazetteer.
_RAW_RECORDS = [
    {
        "pmid": "23300718",
        "title": "Broadly neutralizing human monoclonal antibodies against Chikungunya virus",
        "abstract": (
            "Human monoclonal antibodies from convalescent Chikungunya virus (CHIKV) patients "
            "potently neutralize the virus by targeting the E2 glycoprotein, blocking attachment "
            "and fusion and protecting mice against lethal CHIKV challenge."
        ),
    },
    {
        "pmid": "24672035",
        "title": "A live-attenuated Chikungunya virus vaccine candidate elicits protective immunity",
        "abstract": (
            "A live-attenuated Chikungunya virus vaccine candidate induced durable neutralizing "
            "antibody titers and protected non-human primates from CHIKV viremia and arthritis, "
            "with attenuating deletions in nsP3 and E2."
        ),
    },
    {
        "pmid": "26833106",
        "title": "Neutralizing antibody responses to dengue virus serotypes in natural infection",
        "abstract": (
            "Following natural dengue virus (DENV) infection, neutralizing antibodies target the "
            "envelope protein domain III and confer serotype-specific protection, informing "
            "tetravalent dengue vaccine design."
        ),
    },
    {
        "pmid": "27339099",
        "title": "A tetravalent dengue virus vaccine induces balanced neutralizing immunity",
        "abstract": (
            "A live tetravalent dengue virus vaccine elicited balanced neutralizing antibody "
            "responses against all four DENV serotypes and reduced symptomatic dengue in a "
            "controlled human infection setting."
        ),
    },
    {
        "pmid": "28481359",
        "title": "Structural basis of Chikungunya virus neutralization by a human antibody",
        "abstract": (
            "The crystal structure of a broadly neutralizing human antibody bound to the "
            "Chikungunya virus E2 glycoprotein reveals a conserved quaternary epitope spanning "
            "adjacent envelope spikes, explaining potent CHIKV neutralization and guiding "
            "structure-based immunogen design."
        ),
    },
    {
        "pmid": "29491387",
        "title": "Human antibodies neutralize dengue virus by engaging the envelope dimer epitope",
        "abstract": (
            "Broadly neutralizing human antibodies isolated from dengue virus immune donors "
            "recognize a quaternary envelope dimer epitope on DENV, neutralize all four "
            "serotypes, and protect mice from lethal dengue challenge, defining a target for "
            "dengue vaccine and therapeutic development."
        ),
    },
]

_GAZ_MAP = {
    "chikv": _CHIKV_IRI,
    "chikungunya virus": _CHIKV_IRI,
    "denv": _DENGUE_IRI,
    "dengue virus": _DENGUE_IRI,
}


@pytest.mark.slow
@pytest.mark.skipif(
    not _ollama_reachable(),
    reason=f"Ollama endpoint {_OLLAMA_ROOT} / model {_LLM_MODEL} not reachable",
)
def test_filter_does_not_hurt_precision_real(monkeypatch):
    """Real end-to-end: filter should not reduce mean organism-precision.

    The offline unit is authoritative for the mechanism; this run reports the
    measured filtered-vs-unfiltered numbers and asserts the filter does not hurt.
    """
    pytest.importorskip("sentence_transformers")
    pytest.importorskip("faiss")

    monkeypatch.setenv("APECX_LLM_MODEL", _LLM_MODEL)
    monkeypatch.setenv("APECX_LLM_TEMPERATURE", "0")

    gaz = build_gazetteer(_GAZ_MAP)
    corpus = [stamp_abstract(r, gaz) for r in _RAW_RECORDS]
    # Guard the fixture: each record carries exactly its own organism IRI.
    chikv_iris = [_CHIKV_IRI in r["iris"] for r in corpus]
    dengue_iris = [_DENGUE_IRI in r["iris"] for r in corpus]
    assert sum(chikv_iris) == 3 and sum(dengue_iris) == 3

    cases = [
        {
            "organism": "CHIKV",
            "question": "What antibodies neutralize Chikungunya virus?",
            "correct_iri": _CHIKV_IRI,
        },
        {
            "organism": "DENV",
            "question": "What antibodies neutralize dengue virus?",
            "correct_iri": _DENGUE_IRI,
        },
    ]

    result = run_ablation(cases, corpus, default_reader())

    print(
        "\n[ablation] mean_filtered_precision="
        f"{result['mean_filtered_precision']:.3f} "
        f"mean_unfiltered_precision={result['mean_unfiltered_precision']:.3f}"
    )
    for c in result["per_case"]:
        print(
            f"[ablation] {c['organism']}: "
            f"filtered={c['filtered_precision']:.3f} {c['filtered_citations']} | "
            f"unfiltered={c['unfiltered_precision']:.3f} {c['unfiltered_citations']}"
        )

    assert result["mean_filtered_precision"] >= result["mean_unfiltered_precision"], (
        f"filter must not hurt: {result['mean_filtered_precision']} < "
        f"{result['mean_unfiltered_precision']}"
    )


@pytest.mark.slow
def test_retrieval_reader_is_llm_free_and_on_organism_when_filtered():
    """The retrieval-only reader (no LLM) returns real corpus PMIDs, and over the
    CHIKV-filtered corpus every retrieved paper is on-organism (precision 1.0)."""
    pytest.importorskip("sentence_transformers")
    pytest.importorskip("faiss")

    gaz = build_gazetteer(_GAZ_MAP)
    corpus = [stamp_abstract(r, gaz) for r in _RAW_RECORDS]
    reader = retrieval_reader()

    pmids = {r["pmid"] for r in corpus}
    out = reader("What antibodies neutralize Chikungunya virus?", corpus, k=3)
    assert out["citations"] and all(c in pmids for c in out["citations"])

    result = run_ablation(
        [
            {
                "organism": "CHIKV",
                "question": "What antibodies neutralize Chikungunya virus?",
                "correct_iri": _CHIKV_IRI,
            }
        ],
        corpus,
        reader,
        k=3,
    )
    assert result["mean_filtered_precision"] == 1.0
