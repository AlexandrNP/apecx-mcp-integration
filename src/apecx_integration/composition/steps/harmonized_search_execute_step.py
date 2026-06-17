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

- ``resolution`` — path / canonical_iri / canonical_label / confidence /
  synonyms_count
- ``raw_query`` — q / was_quoted / total / capped / records (the FULL
  matched set, projected) / sample (first 3, for preview) / error /
  ``q_substitution_reason`` (set when ``term`` was an IRI and the
  workflow substituted the canonical label as the raw query; ``None``
  for plain surface-form inputs)
- ``harmonized_query`` — filter_field / filter_shape /
  filter_values_count / filter_values_sample (first 5) / total /
  capped / records (the FULL taxon-filtered set, projected) / sample
  (first 3) / error
- ``divergence`` — absolute_diff / fraction_of_larger_total /
  hitl_recommended
- ``harmonization_health`` (the structured signal the user-facing LLM
  consumes to decide which leg to quote):
  - ``verdict`` ∈ {``"broken"``, ``"degraded"``,
    ``"harmonization_helped"``, ``"healthy_parity"``,
    ``"zero_floor_unclear"``, ``"errored"``}
  - ``reason`` — diagnostic prose describing the bucket
  - ``recommended_total`` — the count the LLM should quote (raw_total
    for ``broken``, 0 for ``zero_floor_unclear``, harm_total otherwise)

When the resolution path is ``ambiguous`` (paused envelope), the parts
shape collapses to ``{resolution, status: "paused_awaiting_disambiguation",
next_action: {kind, param_name, options}}`` — no Globus queries are
executed, and there is no ``raw_query`` / ``harmonized_query`` /
``harmonization_health`` to mis-attribute.

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

from apecx_integration.agents.globus_search._datacite import (
    datacite_identifiers,
    datacite_primary_id,
    datacite_subjects,
    datacite_taxon_iris,
    datacite_title,
)

log = logging.getLogger(__name__)

_INPUT_DU = "plan"
_OUTPUT_KEY = "envelope_input"


# Per-index harmonized filter map. The SC-D ingest has run: every
# *destination* (harmonized) index below is DataCite-shaped and carries
# the canonical pathogen taxon on every record at ``subjects[].valueUri``
# (the NCBI Taxonomy IRI), with each strain record ALSO stamped with its
# species-rank ancestor IRI. So the map has collapsed to the uniform
# ``{field: "subjects.valueUri", shape: "iri"}`` entry the earlier
# source-index version anticipated: a SINGLE canonical IRI filter now
# returns the whole strain set for a species (rename-proof — IRIs are
# stable), replacing the old per-index column filters that enumerated
# every strain *name* against the raw scrape columns.
_HARMONIZED_FILTER: dict[str, dict[str, str]] = {
    "violin_pathogen": {"field": "subjects.valueUri", "shape": "iri"},
    "violin_vaccine": {"field": "subjects.valueUri", "shape": "iri"},
    "violin_gene": {"field": "subjects.valueUri", "shape": "iri"},
    "bvbrc_genome": {"field": "subjects.valueUri", "shape": "iri"},
    "bvbrc_protein": {"field": "subjects.valueUri", "shape": "iri"},
    "bvbrc_protein_structure": {"field": "subjects.valueUri", "shape": "iri"},
    "bvbrc_epitope": {"field": "subjects.valueUri", "shape": "iri"},
    "antiviraldb": {"field": "subjects.valueUri", "shape": "iri"},
    "protabank": {"field": "subjects.valueUri", "shape": "iri"},
}

# Globus Search collection UUIDs — the HARMONIZED *destination* indices
# (the SC-D harmonized output), NOT the raw scrape-input sources. The
# destination carries the DataCite ``subjects[].valueUri`` taxon slot the
# harmonized filter above queries; the source indices have only raw
# per-database columns (no canonical taxon IRI) and must never be the
# query target — pointing here at the sources is what silently broke
# taxon-based search (it fell back to raw name-string matching).
_INDEX_UUIDS: dict[str, str] = {
    "violin_pathogen": "b4965a61-e6de-4e8b-b312-7ab37c7c39d3",
    "violin_vaccine": "12dfce07-0b4a-40b9-8890-48c3e943f9a1",
    "violin_gene": "667dc223-55ba-423a-b116-3bb434813238",
    "bvbrc_genome": "dfefcd85-d130-4dd1-b37a-4bc05f3bcdc8",
    "bvbrc_protein": "826e5d28-c906-4f74-816c-9b37b6ef0a7b",
    "bvbrc_protein_structure": "96fbabbb-06b2-4ea3-91f9-8510bfabb52a",
    "bvbrc_epitope": "4c0b4e3d-1d9d-40be-8cbc-d0f2601e44bf",
    "antiviraldb": "23a7bffd-10b7-4d40-9cec-1a435f32b04e",
    "protabank": "be999b57-88c4-4aff-a883-4b96c57b66cc",
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


# Globus Search offset ceiling: the API enforces ``limit + offset <= 10000``, so a
# single ``limit=10000`` request returns EVERY match up to this hard cap (verified
# live: a 6,687-record query returns all 6,687 in one call). No offset pagination can
# exceed 10,000; deeper result sets need the scroll/marker API (a separate change).
# This replaces the old ``limit=200`` single-page query that silently capped the
# retrieved corpus — the epitope path now carries the FULL matched set downstream so
# the distillation stage ranks the real corpus, not a 3-record preview.
_MAX_RECORDS = 10_000


def _fetch_records(
    client: Any, index_uuid: str, query_body: dict[str, Any]
) -> tuple[int, list[dict[str, Any]], bool]:
    """Run ONE Globus search pulling every match up to the offset ceiling.

    Returns ``(total, records, capped)`` where ``capped`` is True iff the index
    holds more matches than we could retrieve (``total > _MAX_RECORDS``) — an
    honest signal the corpus is the first 10,000, not the whole set.
    """
    resp = client.post_search(index_uuid, {**query_body, "limit": _MAX_RECORDS})
    total = int(resp.data.get("total", 0))
    records = [g["entries"][0]["content"] for g in resp.data.get("gmeta", []) if g.get("entries")]
    return total, records, total > len(records)


def _is_iri_input(term: str) -> bool:
    """True iff ``term`` is an HTTP(S) IRI (a canonical-IRI re-call)."""
    return isinstance(term, str) and term.startswith(("http://", "https://"))


def _select_raw_query_term(term: str, canonical_label: Any) -> tuple[str, str | None]:
    """Pick what to feed Globus as the raw `q=` query string.

    Returns ``(query_term, substitution_reason)``. When the user passed
    an IRI (typically the round-2 re-call after a paused-envelope
    disambiguation), a literal Globus text search for the IRI string
    matches nothing — BV-BRC and VIOLIN don't store IRIs in any indexed
    text field. Substitute the resolved canonical_label so the raw leg
    is a meaningful baseline. When canonical_label is unavailable, fall
    through to the IRI and record the limitation.
    """
    if not _is_iri_input(term):
        return term, None
    if isinstance(canonical_label, str) and canonical_label:
        reason = (
            f"term was an IRI ({term!r}); a literal Globus text search "
            f"for the IRI string would match nothing. Substituting the "
            f"resolved canonical_label {canonical_label!r} as the raw "
            f"query so the raw leg is a meaningful comparison baseline."
        )
        return canonical_label, reason
    return term, (
        f"term was an IRI ({term!r}) but resolver returned no "
        f"canonical_label; raw leg will likely return 0. Treat the raw "
        f"count as not-applicable for this input."
    )


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
    - ``"zero_floor_unclear"`` — both legs returned 0 BUT a filter was
      actually attempted (``filter_values_count >= 1``). Indistinguishable
      between (a) genuinely-rare entity not in this index, (b) broken
      filter at a zero floor (dict labels stale AND raw query happened
      not to match either). Caller should NOT assert "no records exist"
      as a confident answer; surface the ambiguity to the user.
    - ``"healthy_parity"`` — |Δ| within noise floor (< 5 records AND
      < 5%), excluding the zero-floor case above. The two answers agree.
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
    # Zero-floor differentiation: both legs gave 0 but we DID attempt the
    # harmonized filter with >= 1 value. Surface as inconclusive — the
    # entity may be genuinely absent OR both surface forms may be stale.
    if harm_total == 0 and raw_total == 0 and filter_values_count >= 1:
        return "zero_floor_unclear", (
            f"both raw substring search AND harmonized filter on "
            f"`{filter_field}` with {filter_values_count} value(s) "
            f"returned 0 records on index `{index}`. This is ambiguous: "
            f"the entity may be genuinely absent from this index, OR the "
            f"synonym dictionary's canonical labels for "
            f"{canonical_label!r} may not match what this index uses (a "
            f"stale-dict pathology that looks identical to a real miss). "
            f"Do not assert 'no records exist' confidently — recommend "
            f"the user try a broader query (parent species, a related "
            f"surface form, or a different index)."
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
    """Project a harmonized (DataCite-shaped) Globus record to a small preview dict.

    The destination indices store identity in the DataCite shape — the
    human title at ``titles[0].title`` and the taxon/keyword anchors at
    ``subjects[].subject`` — NOT in flat per-database columns. Reading the
    old flat keys (``Species`` / ``Organism`` / …) here returned ``{}`` for
    every harmonized record. ``_datacite`` is the single source of truth
    for pulling these fields, with a flat-key fallback for any record a
    harvester normalized into the simpler shape.
    """
    out: dict[str, Any] = {}
    title = datacite_title(record)
    if title:
        out["title"] = title
    subjects = datacite_subjects(record, limit=4)
    if subjects:
        out["subjects"] = subjects
    # Object references — the concrete IDs a reader needs to trace a claim to a specific
    # database object (PDB / GenBank / UniProt / BVBRC-Genome / DOI). The old code read a
    # top-level ``identifier`` key that DataCite records DO NOT have, so every harmonized
    # record was projected with NO identifier — and the final-doc renderer, which keys off
    # ``subject``, then SKIPPED them entirely (zero Globus records in the evidence ledger).
    identifiers = datacite_identifiers(record)
    if identifiers:
        out["identifiers"] = identifiers
    # ``subject`` = the primary citation token (e.g. "PDB:7H6J"). The renderer requires a
    # string ``subject`` to emit a record; without it the record vanishes from the doc.
    primary = datacite_primary_id(record)
    if primary:
        out["subject"] = primary
    taxon_iris = datacite_taxon_iris(record)
    if taxon_iris:
        out["taxon_iris"] = taxon_iris
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


def _raw_query(index: str, term: str) -> tuple[int, list[dict[str, Any]], str | None]:
    """Run the anonymous RAW full-text query (no IRI / no taxon filter needed) against an
    index, pulling the FULL matched set (up to the Globus offset ceiling). Returns
    ``(total, records, error)``. Used by the resolution-MISS fallback so a term that the
    dictionary can't resolve still PULLS the records that are present in the index —
    instead of returning nothing (the failure: present-but-not-pulled data)."""
    import globus_sdk  # noqa: PLC0415 — heavy import only when actually querying

    uuid = _INDEX_UUIDS.get(index)
    if not uuid:
        return 0, [], f"index {index!r} has no directly-queryable Globus index UUID"
    raw_q, _ = _quote_raw_term(term)
    try:
        total, records, _capped = _fetch_records(globus_sdk.SearchClient(), uuid, {"q": raw_q})
        return total, records, None
    except (globus_sdk.GlobusAPIError, globus_sdk.NetworkError) as exc:
        return 0, [], f"{type(exc).__name__}: {exc}"


def _run_miss_envelope(plan: dict[str, Any]) -> dict[str, Any]:
    """The term did not resolve to a taxon. DO NOT give up — fall back to a RAW full-text query
    so records that are present in the index are still pulled (a resolution miss must not mean
    'no results' when the data exists). Only when the raw query ALSO returns nothing is this a
    genuine no-data answer."""
    index, term = plan["index"], plan["term"]
    raw_total, raw_records, raw_error = _raw_query(index, term)

    if raw_total > 0:
        raw_proj = [_summarize_record(r) for r in raw_records]
        sample = raw_proj[:5]
        md = (
            f"### `{term}` on `{index}` — {raw_total} raw full-text match(es)\n\n"
            f"> ⚠️ The term did NOT resolve to a taxon in the synonym dictionary "
            f"({plan.get('evidence') or 'no entry'}), so taxonomic harmonization was skipped. "
            f"These are UNHARMONIZED full-text matches — relevant records ARE present in the "
            f"index; verify they are the intended organism, and consider a verbose species name "
            f"or an NCBI Taxonomy IRI for a taxon-precise count.\n"
        )
        for s in sample:
            # _summarize_record keeps the DataCite identity fields
            # (title / subjects / identifier); render whichever are present.
            label = s.get("title") or s.get("identifier") or "(record)"
            md += f"- {label}\n"
        bundle_parts = {
            "resolution": {"path": "miss_raw_fallback", "canonical_iri": None, "term": term},
            "raw_total": raw_total,
            "raw_records": raw_proj,  # FULL set for the corpus
            "raw_sample": sample,  # 5-record preview for the markdown
            "harmonization_health": "unharmonized_raw_fallback",
            "status": "ok",
        }
        return {_OUTPUT_KEY: {"markdown": md, "data": {"kind": "bundle", "parts": bundle_parts}}}

    # Raw query ALSO returned nothing (or could not run) — a genuine miss, stated honestly.
    if raw_error:
        why = f"and the raw full-text query could not run ({raw_error})"
    else:
        why = "and a raw full-text query also returned 0 records"
    md = (
        f"### `{term}` on `{index}` — no records\n\n"
        f"The synonym dictionary has no entry for `{term}` "
        f"({plan.get('evidence') or 'no entry'}), {why}. Try a verbose species name or an "
        f"NCBI Taxonomy IRI; if you expected records here, the index may be unreachable."
    )
    bundle_parts = {
        "resolution": {"path": "miss", "canonical_iri": None, "term": term},
        "raw_total": 0,
        "raw_error": raw_error,
        "harmonization_health": "errored" if raw_error else "miss_zero",
        "status": "ok",
    }
    return {_OUTPUT_KEY: {"markdown": md, "data": {"kind": "bundle", "parts": bundle_parts}}}


def _execute_globus_queries(plan: dict[str, Any]) -> dict[str, Any]:
    """Run raw + harmonized queries and emit an ok envelope with divergence."""
    import globus_sdk  # noqa: PLC0415 — heavy import only when actually querying

    index = plan["index"]
    index_uuid = _INDEX_UUIDS[index]
    spec = _HARMONIZED_FILTER[index]
    raw_q_term, raw_q_substitution_reason = _select_raw_query_term(
        plan["term"], plan.get("canonical_label")
    )
    raw_q, was_quoted = _quote_raw_term(raw_q_term)
    filter_values = _build_filter_values(plan)

    client = globus_sdk.SearchClient()

    # Raw query — pull the FULL matched set (up to the Globus offset ceiling), not a page.
    raw_total = 0
    raw_records: list[dict[str, Any]] = []
    raw_error: str | None = None
    raw_capped = False
    try:
        raw_total, raw_records, raw_capped = _fetch_records(client, index_uuid, {"q": raw_q})
    except (
        globus_sdk.GlobusAPIError,
        globus_sdk.NetworkError,
    ) as exc:
        raw_error = f"{type(exc).__name__}: {exc}"
        log.warning("HarmonizedSearchExecuteStep: raw query failed: %s", raw_error)

    # Harmonized query — likewise the FULL taxon-filtered set.
    harm_total = 0
    harm_records: list[dict[str, Any]] = []
    harm_error: str | None = None
    harm_capped = False
    if filter_values:
        try:
            harm_total, harm_records, harm_capped = _fetch_records(
                client,
                index_uuid,
                {
                    "filters": [
                        {
                            "type": "match_any",
                            "field_name": spec["field"],
                            "values": list(filter_values),
                        }
                    ]
                },
            )
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
    if raw_q_substitution_reason:
        md_lines += [
            "",
            f"**Note on raw query**: {raw_q_substitution_reason}",
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
    elif harm_health == "zero_floor_unclear":
        md_lines += [
            "",
            f"**⚠ Inconclusive zero-floor.** {harm_health_reason} Do NOT "
            f"assert 'no records exist' as a confident answer; the most "
            f"honest reply is to surface the ambiguity to the user and "
            f"suggest a broader query (parent species, a related "
            f"surface form, or a different index).",
        ]
    if raw_error:
        md_lines += ["", f"_raw query error_: {raw_error}"]
    if harm_error and harm_health != "broken":
        md_lines += ["", f"_harmonized query error_: {harm_error}"]

    # Project the FULL matched sets once. ``records`` carries the whole corpus
    # downstream (the merge step flattens it into ``globus_results`` for the
    # distillation stage to rank); ``sample`` stays a 3-record preview for the
    # markdown / divergence surface and back-compat.
    raw_proj = [_summarize_record(r) for r in raw_records]
    harm_proj = [_summarize_record(r) for r in harm_records]
    if raw_capped or harm_capped:
        md_lines += [
            "",
            f"**Note**: result set exceeds the Globus offset ceiling "
            f"({_MAX_RECORDS}); the corpus carries the first {_MAX_RECORDS} of "
            f"raw={raw_total}/harmonized={harm_total} matches.",
        ]

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
            "capped": raw_capped,
            "records": raw_proj,
            "sample": raw_proj[:3],
            "error": raw_error,
            "q_substitution_reason": raw_q_substitution_reason,
        },
        "harmonized_query": {
            "filter_field": spec["field"],
            "filter_shape": spec["shape"],
            "filter_values_count": len(filter_values),
            "filter_values_sample": list(filter_values)[:5],
            "total": harm_total,
            "capped": harm_capped,
            "records": harm_proj,
            "sample": harm_proj[:3],
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
            # recommended_total reflects which leg the caller should quote:
            # - broken: raw is the trustworthy signal (dict labels stale)
            # - zero_floor_unclear: neither leg gave a confident count;
            #   surface 0 with explicit caveat
            # - all other verdicts: harm is the canonical answer
            "recommended_total": (
                raw_total
                if harm_health == "broken"
                else 0
                if harm_health == "zero_floor_unclear"
                else harm_total
            ),
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
        index = plan["index"]
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
            # _run_miss_envelope does a SYNC raw Globus query — offload so the loop stays free.
            self.emit_progress(f"searching {index} (unresolved → raw)")
            return await self.run_blocking(_run_miss_envelope, plan)

        log.info(
            "HarmonizedSearchExecuteStep %s: running raw + harmonized Globus queries for path=%s",
            self.name,
            path,
        )
        # _execute_globus_queries does SYNC globus_sdk calls pulling the full corpus
        # (limit=10000 × raw+harmonized). Run it OFF the event loop via run_blocking so a slow
        # Globus call can't freeze the loop (the 2110s gap that starved the desktop keepalive).
        # emit_progress runs from THIS async context, never from inside the threaded fn.
        self.emit_progress(f"searching {index}")
        result = await self.run_blocking(_execute_globus_queries, plan)
        parts = result.get(_OUTPUT_KEY, {}).get("data", {}).get("parts", {})
        if isinstance(parts, dict):
            rt = parts.get("raw_query", {}).get("total")
            ht = parts.get("harmonized_query", {}).get("total")
            self.emit_progress(
                f"{index}: raw {rt} / harmonized {ht}",
                data={"raw_total": rt, "harmonized_total": ht},
            )
        return result
