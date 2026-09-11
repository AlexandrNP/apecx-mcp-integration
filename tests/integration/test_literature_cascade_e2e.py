"""End-to-end integration test for the literature_rag enrich-the-envelope cascade.

Drives ONE ``Workflow.run(input)`` through the WHOLE linear cascade against REAL
components — the real dictionary resolver, the real sentence-transformers retrieval,
and a REAL Ollama LLM synthesis:

    workflow_input
        │  input_to_resolve
        ▼
    resolve_organism_step   organism "Chikungunya virus" -> NCBITaxon_37124
        │  resolve_to_filter    (envelope enriched with target_iri)
        ▼
    ontology_filter_step    keeps only the CHIKV-stamped records, drops dengue
        │  filter_to_rag        (envelope enriched with filtered_records)
        ▼
    literature_rag_step     retrieves + synthesizes a grounded, PMID-cited answer
        │  rag_to_output
        ▼
    workflow_output  {answer, citations, status}

Honesty contract (G127): ``Workflow.run`` SWALLOWS a child-step exception and
returns ``status: 'completed'`` with EMPTY workflow-level outputs. So this test
decides success from the OUTPUT VALUE (a non-empty ``answer`` + ``status == 'ok'``
+ the returned ``citations``), NEVER from the run's ``status`` field. A
``subscribe_to_step_events`` capture surfaces any swallowed ``step_failed`` error
in the assertion message.

The dengue-record assertion is the proof the resolve -> filter leg worked
end-to-end: the resolver turned "Chikungunya virus" into NCBITaxon_37124, the
filter kept only the three records stamped with that IRI, and the dengue record
(stamped NCBITaxon_12637) was excluded — so its PMID can NEVER appear in the
grounded citations.

Gating: skips when Ollama at localhost:11434 is unreachable or the model is
absent; ``importorskip`` on sentence-transformers + faiss (the ``rag`` extra).
Marked ``slow`` — a real mistral-nemo synthesis can exceed 30s.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.slow]

# The retrieval leg needs sentence-transformers; faiss ships in the same `rag`
# extra (import-guard both so a clean install without the extra skips, not errors).
pytest.importorskip("sentence_transformers")
pytest.importorskip("faiss")

from nanobrain.core.step_events import subscribe_to_step_events  # noqa: E402
from nanobrain.core.workflow import Workflow  # noqa: E402

WORKFLOW_YAML = (
    Path(__file__).resolve().parents[1].parent
    / "src"
    / "apecx_integration"
    / "composition"
    / "workflows"
    / "literature_rag"
    / "literature_rag_workflow.yml"
)

_OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
# The task guarantees mistral-nemo is up; pin it so retrieval + the citation gate
# get a capable model regardless of the synthesis-path default.
_MODEL = os.environ.get("APECX_LLM_MODEL", "mistral-nemo:latest")

_CHIKV_IRI = "http://purl.obolibrary.org/obo/NCBITaxon_37124"
_DENGUE_IRI = "http://purl.obolibrary.org/obo/NCBITaxon_12637"

# Three CHIKV abstracts (distinct PMIDs) — antibody / vaccine / genome — each
# stamped with the CHIKV NCBITaxon IRI, plus one dengue abstract the filter drops.
_CHIKV_PMIDS = ["40000001", "40000002", "40000003"]
_DENGUE_PMID = "40009999"

_STAMPED_RECORDS = [
    {
        "pmid": _CHIKV_PMIDS[0],
        "iris": [_CHIKV_IRI],
        "title": "Neutralizing monoclonal antibodies against Chikungunya virus E2",
        "abstract": (
            "Human monoclonal antibodies targeting the Chikungunya virus E2 "
            "glycoprotein potently neutralize infection in vitro and protect mice "
            "from arthritis. The antibodies block viral attachment and fusion, "
            "identifying the E2 domain B as the dominant neutralizing epitope."
        ),
    },
    {
        "pmid": _CHIKV_PMIDS[1],
        "iris": [_CHIKV_IRI],
        "title": "A virus-like-particle vaccine elicits neutralizing antibodies to Chikungunya virus",
        "abstract": (
            "A Chikungunya virus virus-like-particle vaccine induced high titers of "
            "neutralizing antibodies in a phase 1 trial. Serum from vaccinated "
            "subjects neutralized multiple CHIKV genotypes, and passive transfer "
            "conferred protection, supporting antibody-mediated immunity."
        ),
    },
    {
        "pmid": _CHIKV_PMIDS[2],
        "iris": [_CHIKV_IRI],
        "title": "Genome organization of Chikungunya virus and structural protein processing",
        "abstract": (
            "The Chikungunya virus positive-sense RNA genome encodes nonstructural "
            "and structural polyproteins. Processing yields the E1 and E2 envelope "
            "glycoproteins that form the spikes recognized by neutralizing "
            "antibodies, linking genome organization to antibody targets."
        ),
    },
    {
        "pmid": _DENGUE_PMID,
        "iris": [_DENGUE_IRI],
        "title": "Neutralizing antibodies against dengue virus envelope protein",
        "abstract": (
            "Neutralizing monoclonal antibodies against the dengue virus envelope "
            "protein domain III block infection of all four serotypes. This record "
            "concerns dengue virus, NOT Chikungunya virus, and must be excluded by "
            "the NCBITaxon-IRI filter."
        ),
    },
]

_INPUT_ENVELOPE = {
    "organism": "Chikungunya virus",
    "question": "What antibodies neutralize Chikungunya virus?",
    "stamped_records": _STAMPED_RECORDS,
}


def _ollama_reachable() -> bool:
    try:
        r = httpx.get(f"{_OLLAMA_URL}/api/tags", timeout=2.0)
        r.raise_for_status()
        names = {m["name"] for m in r.json().get("models", [])}
        return _MODEL in names
    except Exception:
        return False


@pytest.mark.skipif(
    not _ollama_reachable(),
    reason=f"Ollama at {_OLLAMA_URL} unreachable or model {_MODEL!r} not pulled",
)
async def test_literature_cascade_end_to_end(monkeypatch):
    """resolve -> filter -> read flows end-to-end; the grounded citations are a
    subset of the CHIKV PMIDs and exclude the dengue PMID."""
    monkeypatch.setenv("APECX_LLM_MODEL", _MODEL)

    wf = Workflow.from_config(str(WORKFLOW_YAML))
    await wf.initialize()

    step_failures: list = []

    def _capture(ev) -> None:
        if getattr(ev, "event_type", None) == "step_failed":
            step_failures.append(ev)

    with subscribe_to_step_events(_capture):
        result = await wf.run(
            {"workflow_input": _INPUT_ENVELOPE},
            timeout=180.0,
            settle_ms=500,
        )

    # G127: decide success from the OUTPUT VALUE, not run status. Surface any
    # swallowed step_failed error to make a real failure diagnosable.
    failure_detail = "; ".join(
        f"{getattr(ev, 'step_name', '?')}: {(ev.payload or {}).get('exception', ev.payload)}"
        for ev in step_failures
    )
    out = result.get("workflow_output")
    assert out is not None, (
        f"workflow_output is None — cascade produced nothing. run status="
        f"{result.get('status')!r}; step_failed events=[{failure_detail}]"
    )
    assert out.get("status") == "ok", (
        f"expected reader status 'ok', got {out.get('status')!r}. "
        f"step_failed events=[{failure_detail}]; answer={out.get('answer')!r}"
    )
    assert isinstance(out.get("answer"), str) and out["answer"].strip(), (
        f"expected a non-empty grounded answer; got {out.get('answer')!r}"
    )

    citations = out.get("citations") or []
    assert citations, f"expected at least one grounded PMID citation; got {citations!r}"
    assert set(citations) <= set(_CHIKV_PMIDS), (
        f"citations must be a subset of the CHIKV PMIDs {_CHIKV_PMIDS}; got {citations!r}"
    )
    assert _DENGUE_PMID not in citations, (
        f"dengue PMID {_DENGUE_PMID} must be excluded by the NCBITaxon-IRI filter; "
        f"got citations {citations!r}"
    )
