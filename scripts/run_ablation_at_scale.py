"""Run the with-filter vs without-filter organism ablation on a MIXED real corpus.

EXPERIMENT RUN — not a re-implementation. Every piece of pipeline logic is
reused from ``agents.literature`` (already on main):

  - ``gazetteer.build_from_dictionary`` — surface -> NCBITaxon IRI tagger.
  - ``resolve.resolve_organism_to_iri`` — organism name -> canonical IRI.
  - ``pipeline.harvest_and_stamp`` — bounded PubMed harvest + taxon stamping.
  - ``ablation.{run_ablation, default_reader, organism_precision}`` — the harness
    (retrieve + synthesize + cite, filtered vs whole mixed corpus, precision).

What this script adds is ONLY the orchestration: build one shared gazetteer,
harvest six organisms into ONE pooled corpus (so the unfiltered condition sees
cross-organism papers — the point of the ablation), form two cases per organism,
run the harness, and write the REAL numbers to a JSON. Nothing is tuned to force
filtered >= unfiltered; a synthesis-gate failure (too few filtered records -> a
short answer -> ``synthesis_failed`` -> 0 citations) is recorded as a real data
point, never hidden.

Run:
    cd /Users/onarykov/Downloads/apecx-cowork/wt-lit-scale
    PYTHONPATH=src \
      /Users/onarykov/Downloads/apecx-cowork/apecx-mcp-integration/.venv/bin/python \
      scripts/run_ablation_at_scale.py
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apecx_integration.agents.literature.ablation import default_reader, run_ablation
from apecx_integration.agents.literature.gazetteer import Gazetteer, build_from_dictionary
from apecx_integration.agents.literature.pipeline import harvest_and_stamp
from apecx_integration.agents.literature.resolve import resolve_organism_to_iri

# --------------------------------------------------------------------------- #
# Experiment configuration (real backends only).
# --------------------------------------------------------------------------- #

_DICT_PATH = os.environ.get("APECX_SYNONYM_DICT_PATH") or (
    "/Users/onarykov/Downloads/apecx-cowork/dictionary.sqlite"
)

# The target organisms and a FALLBACK NCBITaxon IRI used ONLY if the live
# dictionary resolution misses (resolve_organism_to_iri is the source of truth).
_TARGETS: list[tuple[str, str]] = [
    ("Chikungunya virus", "http://purl.obolibrary.org/obo/NCBITaxon_37124"),
    ("Dengue virus", "http://purl.obolibrary.org/obo/NCBITaxon_12637"),
    ("Eastern equine encephalitis virus", "http://purl.obolibrary.org/obo/NCBITaxon_11021"),
    ("Rift Valley fever virus", "http://purl.obolibrary.org/obo/NCBITaxon_11588"),
    ("SARS-CoV-2", "http://purl.obolibrary.org/obo/NCBITaxon_2697049"),
    ("Influenza A virus", "http://purl.obolibrary.org/obo/NCBITaxon_11320"),
]

# Gazetteer build limits to try IN ORDER, FULL-WIDTH first so EVERY target tags.
# A high bound (1.4M) covers the whole pathogen table on the current dictionary
# (RVFV's surface sits ~row 1.05M, Dengue was missed at 350k); the whole-table
# escalation (None) is the belt-and-braces fallback. The gazetteer is still built
# ONCE for the harvest loop — this only picks the limit.
_GAZ_LIMITS: list[int | None] = [1_400_000, None]

_MAX_PAPERS = 30
_RETRIEVAL_K = 5
# A target becomes a case only if the pooled corpus carries at least this many
# records stamped with its IRI (otherwise the filtered condition is empty).
_MIN_STAMPED_FOR_CASE = 1

# Two varied real questions per qualifying organism (up to 6*2 = 12 cases). One
# probes antibodies/vaccines, the other pathogenesis/transmission — different
# retrieval targets over the same stamped corpus.
_QUESTION_TEMPLATES: list[str] = [
    "What is known about {name} and neutralizing antibodies or vaccines?",
    "What is known about {name} pathogenesis and transmission?",
]

_RESULTS_PATH = Path(__file__).resolve().parent.parent / "docs" / "literature_ablation_results.json"


def _is_tagged(gaz: Gazetteer, name: str, iri: str) -> bool:
    """True iff tagging the organism NAME yields its resolved IRI (confirms the
    surface->IRI mapping is present at the chosen gazetteer limit)."""
    return any(tag.iri == iri for tag in gaz.tag(name))


def _build_gazetteer_covering_targets(
    resolved: list[tuple[str, str]],
) -> tuple[Gazetteer, int | None, dict[str, bool]]:
    """Build one gazetteer, escalating the row limit until every target is tagged
    (or the limits are exhausted). Returns (gazetteer, limit_used, tagged_map)."""
    tagged: dict[str, bool] = {}
    gaz: Gazetteer | None = None
    limit_used: int | None = None
    for limit in _GAZ_LIMITS:
        gaz = build_from_dictionary(_DICT_PATH, limit=limit)
        limit_used = limit
        tagged = {name: _is_tagged(gaz, name, iri) for name, iri in resolved}
        print(f"[gazetteer] limit={limit}: tagged={tagged}")
        if all(tagged.values()):
            break
    assert gaz is not None
    return gaz, limit_used, tagged


def main() -> None:
    # Force the target model regardless of shell env (default is nemotron-3-nano).
    os.environ["APECX_LLM_MODEL"] = os.environ.get("APECX_LLM_MODEL") or "mistral-nemo:latest"
    os.environ.setdefault("APECX_LLM_TEMPERATURE", "0")
    model = os.environ["APECX_LLM_MODEL"]

    print(f"[config] dict={_DICT_PATH}")
    print(f"[config] model={model} base_url={os.environ.get('APECX_LLM_BASE_URL') or 'default'}")

    # 1. Resolve each organism's IRI (dict source of truth; fallback on a miss).
    resolved: list[tuple[str, str]] = []
    resolution_source: dict[str, str] = {}
    for name, fallback in _TARGETS:
        iri = resolve_organism_to_iri(name, _DICT_PATH)
        resolution_source[name] = "dictionary" if iri else "fallback"
        resolved.append((name, iri or fallback))
        print(f"[resolve] {name} -> {iri or fallback} ({resolution_source[name]})")

    # 2. Build ONE shared gazetteer that tags every target (escalate limit if needed).
    gaz, gaz_limit, tagged = _build_gazetteer_covering_targets(resolved)
    untagged = [name for name, ok in tagged.items() if not ok]
    if untagged:
        print(f"[gazetteer] WARNING untagged after all limits: {untagged} (skipped as cases)")

    # 3. Harvest each organism and POOL into one mixed corpus.
    pooled: list[dict] = []
    harvested_counts: dict[str, dict[str, int]] = {}
    for name, iri in resolved:
        records = harvest_and_stamp(name, gaz, max_papers=_MAX_PAPERS)
        stamped_for_iri = sum(1 for r in records if iri in (r.get("iris") or []))
        harvested_counts[name] = {
            "harvested": len(records),
            "stamped_with_own_iri": stamped_for_iri,
        }
        pooled.extend(records)
        print(f"[harvest] {name}: {len(records)} records, {stamped_for_iri} stamped with own IRI")

    # 4. Two cases per organism that is tagged AND has enough stamped records.
    cases: list[dict] = []
    skipped: list[dict] = []
    for name, iri in resolved:
        stamped = harvested_counts[name]["stamped_with_own_iri"]
        if not tagged.get(name, False):
            skipped.append({"organism": name, "reason": "not tagged at any gazetteer limit"})
            continue
        if stamped < _MIN_STAMPED_FOR_CASE:
            skipped.append(
                {
                    "organism": name,
                    "reason": f"only {stamped} stamped records (< {_MIN_STAMPED_FOR_CASE})",
                }
            )
            continue
        for template in _QUESTION_TEMPLATES:
            cases.append(
                {
                    "organism": name,
                    "question": template.format(name=name),
                    "correct_iri": iri,
                }
            )

    print(f"[cases] {len(cases)} cases, {len(skipped)} skipped: {skipped}")
    if not cases:
        raise SystemExit("No usable cases — cannot run ablation (see skipped reasons above).")

    # 5. Run the harness. Wrap default_reader to CAPTURE each call's status
    #    honestly (run_ablation itself keeps only citations). Calls are ordered
    #    per case: filtered first, then unfiltered — so statuses pair up 2-by-2.
    base_reader = default_reader()
    call_statuses: list[str | None] = []

    def reader(question: str, records: list[dict], *, k: int = _RETRIEVAL_K) -> dict[str, Any]:
        result = base_reader(question, records, k=k)
        call_statuses.append(result.get("status"))
        return result

    result = run_ablation(cases, pooled, reader, k=_RETRIEVAL_K)

    # Attach captured statuses + filtered-corpus size to each per_case entry.
    for i, case in enumerate(result["per_case"]):
        correct_iri = case["correct_iri"]
        case["filtered_corpus_size"] = sum(
            1 for r in pooled if correct_iri in (r.get("iris") or [])
        )
        case["filtered_status"] = call_statuses[2 * i] if 2 * i < len(call_statuses) else None
        case["unfiltered_status"] = (
            call_statuses[2 * i + 1] if 2 * i + 1 < len(call_statuses) else None
        )

    # 6. Assemble the full result record and write it.
    record = {
        "experiment": "literature_organism_filter_ablation_at_scale",
        "timestamp": datetime.now(UTC).isoformat(),
        "model": model,
        "llm_base_url": os.environ.get("APECX_LLM_BASE_URL")
        or "default(http://localhost:11434/v1)",
        "dictionary_path": _DICT_PATH,
        "gazetteer_limit_used": gaz_limit,
        "gazetteer_tagged": tagged,
        "max_papers_per_organism": _MAX_PAPERS,
        "retrieval_k": _RETRIEVAL_K,
        "corpus_size": len(pooled),
        "resolution_source": resolution_source,
        "harvested_counts": harvested_counts,
        "skipped_organisms": skipped,
        "mean_filtered_precision": result["mean_filtered_precision"],
        "mean_unfiltered_precision": result["mean_unfiltered_precision"],
        "n_cases": result["n_cases"],
        "per_case": result["per_case"],
        "note": (
            "Honest run: numbers are exactly as produced. 0 citations with "
            "status 'synthesis_failed' or 'no_evidence' means the synthesizer's "
            "min-length / no-evidence gate fired (too few filtered records) — a "
            "real data point, not tuned away."
        ),
    }

    _RESULTS_PATH.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    # 7. Print the headline numbers.
    print("\n===== ABLATION RESULT =====")
    print(f"corpus_size={len(pooled)}  n_cases={result['n_cases']}")
    print(f"mean_filtered_precision   = {result['mean_filtered_precision']:.3f}")
    print(f"mean_unfiltered_precision = {result['mean_unfiltered_precision']:.3f}")
    for c in result["per_case"]:
        print(
            f"  {c['organism']}: "
            f"filtered={c['filtered_precision']:.3f} "
            f"(status={c['filtered_status']}, corpus={c['filtered_corpus_size']}) "
            f"cites={c['filtered_citations']} | "
            f"unfiltered={c['unfiltered_precision']:.3f} "
            f"(status={c['unfiltered_status']}) cites={c['unfiltered_citations']}"
        )
    print(f"\nWrote {_RESULTS_PATH}")


if __name__ == "__main__":
    main()
