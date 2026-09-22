"""Headline ablation: does the organism-identifier filter improve the answers?

Two public functions:

  - :func:`organism_precision` — score ONE condition's citations: the fraction of
    cited PMIDs whose stamped record's ``iris`` contains the case's ``correct_iri``.
  - :func:`run_ablation` — run a dependency-INJECTED ``reader`` over each case
    twice (once on the IRI-filtered corpus, once on the whole mixed corpus) and
    report mean organism-precision per condition.

The reader is injected — ``reader(question, records) -> {"citations": [pmid,...],
...}`` — so the harness itself is unit-testable with a trivial fake reader and no
LLM. :func:`default_reader` adapts the real ``LiteratureRagStep`` (retrieve +
synthesize + cite) into that callable for the live run.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

#: A reader retrieves + synthesizes an answer over ``records`` for ``question``
#: and returns at least ``{"citations": [pmid, ...]}``. ``k`` (retrieval budget)
#: is passed as a keyword; a reader that cannot honour it may ignore it.
Reader = Callable[..., dict[str, Any]]


def organism_precision(
    citations: list[str],
    corpus_by_pmid: dict[str, dict],
    correct_iri: str,
) -> float:
    """Fraction of cited PMIDs whose stamped record's ``iris`` contains
    ``correct_iri``. Returns ``0.0`` when there are no citations."""
    if not citations:
        return 0.0
    hits = sum(
        1
        for pmid in citations
        if correct_iri in (corpus_by_pmid.get(str(pmid), {}).get("iris") or [])
    )
    return hits / len(citations)


def run_ablation(
    cases: list[dict],
    corpus: list[dict],
    reader: Reader,
    *,
    k: int = 5,
) -> dict[str, Any]:
    """Run ``reader`` filtered-vs-unfiltered over each case; report mean precision.

    ``cases`` are ``{organism, question, correct_iri}``; ``corpus`` are stamped
    records (each with ``pmid`` + ``iris``). For each case the FILTERED condition
    keeps only records whose ``iris`` contains ``correct_iri``; the UNFILTERED
    condition uses the whole mixed corpus. ``k`` is the retrieval budget handed to
    the reader. Returns ``{per_case, mean_filtered_precision,
    mean_unfiltered_precision, n_cases}``.
    """
    corpus_by_pmid = {str(r["pmid"]): r for r in corpus}
    per_case: list[dict[str, Any]] = []
    for case in cases:
        question = case["question"]
        correct_iri = case["correct_iri"]
        records_f = [r for r in corpus if correct_iri in (r.get("iris") or [])]

        filtered = reader(question, records_f, k=k)
        unfiltered = reader(question, corpus, k=k)

        per_case.append(
            {
                "organism": case.get("organism"),
                "question": question,
                "correct_iri": correct_iri,
                "filtered_citations": filtered.get("citations") or [],
                "unfiltered_citations": unfiltered.get("citations") or [],
                "filtered_precision": organism_precision(
                    filtered.get("citations") or [], corpus_by_pmid, correct_iri
                ),
                "unfiltered_precision": organism_precision(
                    unfiltered.get("citations") or [], corpus_by_pmid, correct_iri
                ),
            }
        )

    n = len(per_case)
    return {
        "per_case": per_case,
        "mean_filtered_precision": (
            sum(c["filtered_precision"] for c in per_case) / n if n else 0.0
        ),
        "mean_unfiltered_precision": (
            sum(c["unfiltered_precision"] for c in per_case) / n if n else 0.0
        ),
        "n_cases": n,
    }


def mine_rag_failures(
    per_case: list[dict],
    corpus_by_pmid: dict[str, dict],
    iri_to_name: dict[str, str],
) -> dict[str, list[dict]]:
    """Extract the CONCRETE citations where the unfiltered ("pure RAG") condition
    fails, turning each aggregate precision drop into a named example.

    A *false positive* is an off-organism paper pure RAG cited: a PMID in a case's
    ``unfiltered_citations`` whose stamped record's ``iris`` does NOT contain the
    case's ``correct_iri``. It is labelled with the organism(s) the paper is
    actually about (mapped from its stamped iris via ``iri_to_name``) and its title
    — a query -> specific wrong record the organism filter would have excluded.

    A *false negative* is an on-organism paper pure RAG missed: a PMID in a case's
    ``filtered_citations`` that is stamped with ``correct_iri`` yet absent from
    ``unfiltered_citations`` — the right paper crowded out of the mixed pile.

    Pure function (no LLM, no network): reads only the ablation ``per_case`` rows
    plus the stamped corpus, so it is unit-testable and re-runnable on a saved run.
    """
    false_positives: list[dict] = []
    false_negatives: list[dict] = []
    for case in per_case:
        correct_iri = case["correct_iri"]
        organism = case.get("organism")
        question = case.get("question")
        unfiltered = [str(p) for p in (case.get("unfiltered_citations") or [])]
        filtered = [str(p) for p in (case.get("filtered_citations") or [])]
        unfiltered_set = set(unfiltered)

        for pmid in unfiltered:
            record = corpus_by_pmid.get(pmid, {})
            iris = record.get("iris") or []
            if correct_iri not in iris:
                actual = sorted({iri_to_name.get(i, i) for i in iris}) or ["unknown"]
                false_positives.append(
                    {
                        "organism": organism,
                        "question": question,
                        "cited_pmid": pmid,
                        "title": record.get("title", ""),
                        "actual_organisms": actual,
                    }
                )

        for pmid in filtered:
            record = corpus_by_pmid.get(pmid, {})
            iris = record.get("iris") or []
            if correct_iri in iris and pmid not in unfiltered_set:
                false_negatives.append(
                    {
                        "organism": organism,
                        "question": question,
                        "missed_pmid": pmid,
                        "title": record.get("title", ""),
                    }
                )

    return {"false_positives": false_positives, "false_negatives": false_negatives}


def default_reader() -> Reader:
    """Adapt the real ``LiteratureRagStep`` into a ``reader(question, records)``.

    Needs the ``rag`` extra (sentence-transformers + faiss) for retrieval and a
    reachable LLM (``APECX_LLM_*``) for synthesis. The step retrieves a fixed
    top-5 internally, so the reader accepts ``k`` for the contract but does not
    forward it. The returned callable maps ``{answer, citations, status}`` from
    the step straight through (the harness reads only ``citations``).
    """
    from apecx_integration.composition.workflows.literature_rag.steps import (
        literature_rag_step as lrs,
    )

    step_yaml = Path(lrs.__file__).resolve().parent / "literature_rag_step.yml"
    step = lrs.LiteratureRagStep.from_config(str(step_yaml))

    def _read(question: str, records: list[dict], *, k: int = 5) -> dict[str, Any]:
        return asyncio.run(step.process({"question": question, "filtered_records": records}))

    return _read
