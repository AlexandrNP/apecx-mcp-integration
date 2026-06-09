"""HarmonizedSearchExecuteStep — run raw + harmonized Globus queries, emit envelope-shaped output.

Second step of the harmonized_search workflow. Takes the plan produced
by ``HarmonizedResolveStep`` and either:

- emits a *paused* envelope (markdown + Bundle data) carrying the
  candidate list when the resolution is ambiguous — the Globus queries
  are NOT executed, so the LLM has no harmonized record count to
  silently mis-attribute; OR
- runs both a raw (``q=<term>``) and a harmonized (filter by per-index
  field with canonical_label / NCBI_Taxonomy_ID values) Globus query,
  computes divergence, and emits an *ok* envelope.

The per-index filter map (``_HARMONIZED_FILTER``) is the single source
of truth for which field/shape to filter on per Globus index. It mirrors
the standalone script at
``apecx-harvesters-work/search_demo/agent-skill-harmonized/scripts/harmonized_query.py``
but lives in apecx_integration so it's reachable from the MCP tool +
test surfaces.

Input contract (under data unit ``plan``): the dict emitted by
``HarmonizedResolveStep``.

Output (under ``envelope_input``): the dict shape ``EnvelopeStep``
consumes — ``{markdown: str, data: dict (serialized DataShape)}``. The
``data`` Bundle's parts carry:

- ``resolution`` — path / canonical_iri / candidates / synonyms_count
- ``raw_query`` — q / was_quoted / total / sample (first 3 records)
- ``harmonized_query`` — filter_field / filter_shape / filter_values
  (sample) / total / sample
- ``divergence`` — absolute_diff / fraction_of_larger_total / hitl_required

Compliance notes:
- ``from_config``-only construction; subclass of ``BaseStep``.
- Implements ``process()``; does NOT override ``execute()``.
- Network failures from globus_sdk are caught and surfaced as
  ``raw_total = 0`` / ``harmonized_total = 0`` with the error string
  recorded in evidence — the workflow must always emit a WorkflowResult.
"""

from __future__ import annotations

import logging
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig

log = logging.getLogger(__name__)

_INPUT_DU = "plan"
_OUTPUT_KEY = "envelope_input"


# Per-index harmonized filter map. Mirrors the standalone CLI's
# HARMONIZED_FILTER (apecx-harvesters skill). When the SC-D ingest
# eventually populates ``subjects.valueUri`` on every published index,
# this collapses to a uniform ``{field: "subjects.valueUri", shape: "iri"}``
# entry per index.
_HARMONIZED_FILTER: dict[str, dict[str, str]] = {
    "violin_pathogen": {"field": "NCBI_Taxonomy_ID", "shape": "taxon_id"},
    "violin_vaccine": {"field": "VIOLIN_c_pathogen_id", "shape": "taxon_id"},
    "violin_gene": {"field": "Organism", "shape": "label"},
    "bvbrc_genome": {"field": "Species", "shape": "label"},
    "bvbrc_protein": {"field": "Genome", "shape": "label"},
    "bvbrc_protein_structure": {"field": "Organism_Name", "shape": "label"},
    "bvbrc_epitope": {"field": "Organism", "shape": "label"},
    "antiviraldb": {"field": "Virus", "shape": "label"},
    "protabank": {"field": "Title", "shape": "label"},
}

# Globus Search collection UUIDs for each APECx index.
_INDEX_UUIDS: dict[str, str] = {
    "violin_pathogen": "a67c7310-5115-446f-bfb6-d889bc4efa06",
    "violin_vaccine": "c5ff64fd-5e78-4cf0-848a-2788a78e71cd",
    "violin_gene": "205c1a5b-c9bd-4137-8ac6-ca879c9a4f9c",
    "bvbrc_genome": "b676edbe-3286-4514-bc13-5cbe891c4bb1",
    "bvbrc_protein": "249efe96-14d2-443d-ad47-5621ed43a343",
    "bvbrc_protein_structure": "439f2b66-09d4-4141-8c3d-b4dc18ef8a07",
    "bvbrc_epitope": "f873c7d5-8652-466d-806b-b5da46f0f786",
    "antiviraldb": "e8097a7b-a280-4031-9df1-1e837193494f",
    "protabank": "9e902471-9c77-49d3-a12c-516cc0808c3b",
}


def _quote_raw_term(term: str) -> tuple[str, bool]:
    """Wrap multi-token / non-alphanumeric terms in double quotes.

    Globus full-text tokenizes on whitespace AND non-alphanumeric chars,
    so ``q=HSV-2`` matches every record containing ``HSV`` OR ``2``.
    Quoting enforces a phrase match.
    """
    needs = any(not c.isalnum() for c in term)
    if needs and not (term.startswith('"') and term.endswith('"')):
        return f'"{term}"', True
    return term, False


def _iri_to_taxon_id(iri: str) -> int | None:
    """Extract integer NCBI Taxonomy ID from an OBO IRI tail."""
    suffix = iri.rsplit("/", 1)[-1].split("_", 1)[-1]
    try:
        return int(suffix)
    except (TypeError, ValueError):
        return None


def _build_filter_values(plan: dict[str, Any]) -> list[Any]:
    """Build the harmonized filter values per the index's shape spec."""
    index = plan["index"]
    spec = _HARMONIZED_FILTER[index]
    shape = spec["shape"]

    if shape == "label":
        out: list[str] = []
        label = plan.get("canonical_label")
        if isinstance(label, str) and label:
            out.append(label)
        for syn in plan.get("synonyms", []) or []:
            if isinstance(syn, str) and syn and syn not in out:
                out.append(syn)
        return out

    if shape == "taxon_id":
        out_taxa: list[int] = []
        iri = plan.get("canonical_iri")
        if isinstance(iri, str) and iri:
            tid = _iri_to_taxon_id(iri)
            if tid is not None:
                out_taxa.append(tid)
        return out_taxa

    # shape == "iri" (future SC-D)
    iri = plan.get("canonical_iri")
    return [iri] if isinstance(iri, str) and iri else []


def _compute_harmonization_health(
    raw_total: int,
    harm_total: int,
    filter_field: str,
    filter_values_count: int,
    index: str,
    canonical_label: Any,
    raw_error: str | None,
    harm_error: str | None,
) -> tuple[str, str]:
    """Classify the harmonization outcome into a structured verdict.

    Pure function — no Globus dependency. Returns ``(verdict, reason)``.

    Verdicts:
    - ``"broken"`` — harm filter returned 0 records but raw matched some.
      The dictionary's canonical labels don't match what the index uses
      (commonly: stale ICTV taxonomy rename, e.g. "Yellow fever virus"
      vs. "Orthoflavivirus flavi"). Caller should defer to raw results.
    - ``"degraded"`` — harm < raw but harm > 0. Filter caught the
      canonical match shape but missed records whose surface form
      doesn't fit any known synonym. Raw is the broader signal.
    - ``"harmonization_helped"`` — harm > raw. The synonym expansion
      reaches records that raw substring search missed (the canonical
      win case the workflow was designed to surface).
    - ``"healthy_parity"`` — |Δ| within noise floor (< 5 records AND
      < 5%). The two answers agree.
    - ``"errored"`` — raw or harm query raised; caller has the error
      text in raw_error / harm_error.
    """
    if raw_error or harm_error:
        return "errored", (f"query error: raw_error={raw_error!r}, harm_error={harm_error!r}")

    absolute_diff = abs(raw_total - harm_total)
    larger = max(raw_total, harm_total) or 1
    divergence_fraction = absolute_diff / larger
    diverges = absolute_diff >= 5 or divergence_fraction >= 0.05

    if harm_total == 0 and raw_total > 0:
        return "broken", (
            f"harmonized filter on `{filter_field}` with "
            f"{filter_values_count} value(s) returned 0 records while raw "
            f"substring matched {raw_total}. The synonym dictionary's "
            f"canonical labels for this entity do not match the values "
            f"used by index `{index}` (commonly a stale ICTV taxonomy "
            f"rename). Use the raw query results."
        )
    if harm_total > raw_total:
        return "harmonization_helped", (
            f"harmonized filter reached {harm_total - raw_total} additional "
            f"records the raw substring search missed."
        )
    if diverges:
        return "degraded", (
            f"harmonized filter caught {harm_total} records; raw caught "
            f"{raw_total} ({absolute_diff} additional). The raw query "
            f"may be reaching records whose surface form doesn't match "
            f"any synonym for {canonical_label!r}."
        )
    return "healthy_parity", (
        f"|Δ|={absolute_diff} within noise floor (5 records, 5%). Raw and harmonized agree."
    )


def _summarize_record(record: dict[str, Any]) -> dict[str, Any]:
    """Project a Globus record to a small preview dict.

    Strips deeply nested arrays; surfaces the most identity-bearing
    top-level fields per source.
    """
    out: dict[str, Any] = {}
    for k in (
        "Genome_Name",
        "Species",
        "Pathogen",
        "Organism",
        "Organism_Name",
        "Virus",
        "Title",
        "Gene_Name",
        "Genome",
    ):
        if k in record and record[k] is not None:
            out[k] = record[k]
    # DataCite identifier if present.
    ident = record.get("identifier")
    if isinstance(ident, dict) and ident.get("identifier"):
        out["identifier"] = ident["identifier"]
    return out


def _run_paused_envelope(plan: dict[str, Any]) -> dict[str, Any]:
    """Emit a paused envelope when resolution is ambiguous.

    No Globus queries run — the LLM has no record count to silently
    mis-attribute. The candidates are the only structured payload.
    """
    candidates = plan["candidates"]
    md_lines = [
        f"### Term `{plan['term']}` is ambiguous on index `{plan['index']}`",
        "",
        f"The synonym dictionary resolves `{plan['term']}` to "
        f"{len(candidates)} distinct canonical entries. The harmonized "
        f"search has NOT been executed — running it across all candidate "
        f"taxa would lump heterogeneous biology together. Pick one:",
        "",
    ]
    for c in candidates:
        md_lines.append(f"  - `{c['canonical_iri']}` — {c['canonical_label']!r}")
    md_lines += [
        "",
        "Re-call this workflow with the chosen canonical IRI as `term` "
        "(the resolver short-circuits IRI inputs via `path=fast`).",
    ]

    bundle_parts = {
        "resolution": {
            "path": "ambiguous",
            "canonical_iri": None,
            "candidate_count": len(candidates),
            "candidates": candidates,
        },
        "status": "paused_awaiting_disambiguation",
        "next_action": {
            "kind": "re-invoke_with_chosen_iri",
            "param_name": "term",
            "options": [c["canonical_iri"] for c in candidates],
        },
    }
    data = {"kind": "bundle", "parts": bundle_parts}
    return {
        _OUTPUT_KEY: {
            "markdown": "\n".join(md_lines),
            "data": data,
        }
    }


def _run_miss_envelope(plan: dict[str, Any]) -> dict[str, Any]:
    """Emit a miss envelope — dictionary had no entry for the term."""
    md = (
        f"### Term `{plan['term']}` did not resolve on index `{plan['index']}`\n\n"
        f"The synonym dictionary has no entry that matches "
        f"`{plan['term']}`. Evidence: {plan.get('evidence') or '(none)'}.\n\n"
        f"Try a different surface form, a verbose species name, or an "
        f"NCBI Taxonomy IRI."
    )
    bundle_parts = {
        "resolution": {
            "path": "miss",
            "canonical_iri": None,
            "term": plan["term"],
        },
        "status": "ok",
        "raw_query_skipped_reason": "No canonical IRI; harmonized query has no filter values.",
    }
    data = {"kind": "bundle", "parts": bundle_parts}
    return {
        _OUTPUT_KEY: {
            "markdown": md,
            "data": data,
        }
    }


def _execute_globus_queries(plan: dict[str, Any]) -> dict[str, Any]:
    """Run raw + harmonized queries and emit an ok envelope with divergence."""
    import globus_sdk  # noqa: PLC0415 — heavy import only when actually querying

    index = plan["index"]
    index_uuid = _INDEX_UUIDS[index]
    spec = _HARMONIZED_FILTER[index]
    raw_q, was_quoted = _quote_raw_term(plan["term"])
    filter_values = _build_filter_values(plan)

    client = globus_sdk.SearchClient()

    # Raw query
    raw_total = 0
    raw_records: list[dict[str, Any]] = []
    raw_error: str | None = None
    try:
        resp = client.post_search(index_uuid, {"q": raw_q, "limit": 200})
        raw_total = int(resp.data.get("total", 0))
        raw_records = [
            g["entries"][0]["content"] for g in resp.data.get("gmeta", []) if g.get("entries")
        ]
    except (
        globus_sdk.GlobusAPIError,
        globus_sdk.NetworkError,
    ) as exc:
        raw_error = f"{type(exc).__name__}: {exc}"
        log.warning("HarmonizedSearchExecuteStep: raw query failed: %s", raw_error)

    # Harmonized query
    harm_total = 0
    harm_records: list[dict[str, Any]] = []
    harm_error: str | None = None
    if filter_values:
        try:
            resp = client.post_search(
                index_uuid,
                {
                    "filters": [
                        {
                            "type": "match_any",
                            "field_name": spec["field"],
                            "values": list(filter_values),
                        }
                    ],
                    "limit": 200,
                },
            )
            harm_total = int(resp.data.get("total", 0))
            harm_records = [
                g["entries"][0]["content"] for g in resp.data.get("gmeta", []) if g.get("entries")
            ]
        except (
            globus_sdk.GlobusAPIError,
            globus_sdk.NetworkError,
        ) as exc:
            harm_error = f"{type(exc).__name__}: {exc}"
            log.warning(
                "HarmonizedSearchExecuteStep: harmonized query failed: %s",
                harm_error,
            )
    else:
        harm_error = "no filter values built (resolution missed)"

    # Divergence
    absolute_diff = abs(raw_total - harm_total)
    larger = max(raw_total, harm_total) or 1
    divergence_fraction = absolute_diff / larger
    diverges = absolute_diff >= 5 or divergence_fraction >= 0.05

    harm_health, harm_health_reason = _compute_harmonization_health(
        raw_total=raw_total,
        harm_total=harm_total,
        filter_field=spec["field"],
        filter_values_count=len(filter_values),
        index=index,
        canonical_label=plan.get("canonical_label"),
        raw_error=raw_error,
        harm_error=harm_error,
    )

    # Markdown summary
    md_lines = [
        f"### Harmonized search: `{plan['term']}` on `{index}`",
        "",
        f"- Resolved to: `{plan['canonical_iri']}` ({plan.get('canonical_label')!r})",
        f"- Raw query `q={raw_q}`: **{raw_total}** record(s)",
        f"- Harmonized (`{spec['field']}` × {len(filter_values)} value(s)): "
        f"**{harm_total}** record(s)",
        f"- Divergence: |Δ|={absolute_diff} ({divergence_fraction:.0%} of {larger})",
        f"- Harmonization health: **{harm_health}**",
    ]
    if harm_health == "broken":
        md_lines += [
            "",
            f"**⚠ Harmonization broken for this entity.** {harm_health_reason} "
            f"Quote the raw count ({raw_total}) when answering the user, "
            f"and flag that the synonym dictionary may need a rebuild "
            f"against the current index taxonomy.",
        ]
    elif harm_health == "harmonization_helped":
        md_lines += [
            "",
            f"**Heads-up**: {harm_health_reason} The harmonized superset is "
            f"the better answer; raw substring missed records whose surface "
            f"form doesn't contain `{plan['term']}` literally.",
        ]
    elif harm_health == "degraded":
        md_lines += [
            "",
            f"**Heads-up**: {harm_health_reason} Present both sides "
            f"(raw={raw_total}, harmonized={harm_total}) to the user.",
        ]
    if raw_error:
        md_lines += ["", f"_raw query error_: {raw_error}"]
    if harm_error and harm_health != "broken":
        md_lines += ["", f"_harmonized query error_: {harm_error}"]

    bundle_parts: dict[str, Any] = {
        "resolution": {
            "path": plan["resolution_path"],
            "canonical_iri": plan["canonical_iri"],
            "canonical_label": plan["canonical_label"],
            "confidence": plan["confidence"],
            "synonyms_count": len(plan.get("synonyms", [])),
        },
        "raw_query": {
            "q": raw_q,
            "was_quoted": was_quoted,
            "total": raw_total,
            "sample": [_summarize_record(r) for r in raw_records[:3]],
            "error": raw_error,
        },
        "harmonized_query": {
            "filter_field": spec["field"],
            "filter_shape": spec["shape"],
            "filter_values_count": len(filter_values),
            "filter_values_sample": list(filter_values)[:5],
            "total": harm_total,
            "sample": [_summarize_record(r) for r in harm_records[:3]],
            "error": harm_error,
        },
        "divergence": {
            "absolute_diff": absolute_diff,
            "fraction_of_larger_total": round(divergence_fraction, 4),
            "hitl_recommended": diverges,
        },
        "harmonization_health": {
            "verdict": harm_health,
            "reason": harm_health_reason,
            "recommended_total": (raw_total if harm_health == "broken" else harm_total),
        },
        "status": "ok",
    }
    data = {"kind": "bundle", "parts": bundle_parts}
    return {
        _OUTPUT_KEY: {
            "markdown": "\n".join(md_lines),
            "data": data,
        }
    }


class HarmonizedSearchExecuteStep(BaseStep):
    """Execute the raw + harmonized Globus queries (or emit a paused envelope)."""

    @classmethod
    def _get_config_class(cls):
        return StepConfig

    async def process(self, input_data: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"HarmonizedSearchExecuteStep '{self.name}': input_data "
                f"must be a dict, got {type(input_data).__name__}"
            )

        # Unwrap framework trigger envelope.
        if (
            _INPUT_DU in input_data
            and isinstance(input_data[_INPUT_DU], dict)
            and "resolution_path" not in input_data
        ):
            input_data = input_data[_INPUT_DU]

        plan = input_data
        for required in ("term", "index", "resolution_path"):
            if required not in plan:
                raise ValueError(
                    f"HarmonizedSearchExecuteStep '{self.name}': plan "
                    f"missing required key {required!r}"
                )

        if plan["index"] not in _HARMONIZED_FILTER:
            raise ValueError(
                f"HarmonizedSearchExecuteStep '{self.name}': unknown index "
                f"{plan['index']!r}; expected one of "
                f"{sorted(_HARMONIZED_FILTER)}"
            )

        path = plan["resolution_path"]
        if path == "ambiguous":
            log.info(
                "HarmonizedSearchExecuteStep %s: ambiguous resolution, "
                "emitting paused envelope without Globus queries",
                self.name,
            )
            return _run_paused_envelope(plan)
        if path == "miss":
            log.info(
                "HarmonizedSearchExecuteStep %s: miss, emitting miss envelope",
                self.name,
            )
            return _run_miss_envelope(plan)

        log.info(
            "HarmonizedSearchExecuteStep %s: running raw + harmonized Globus queries for path=%s",
            self.name,
            path,
        )
        return _execute_globus_queries(plan)
