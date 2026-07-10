"""Per-(query × index) live retrieval against the shipped harmonized_search primitives.

Reuses the production functions so the raw/harmonized/served definitions can NEVER drift from what the
product does: `_fetch_records` (the exact post_search), `_INDEX_UUIDS`/`_HARMONIZED_FILTER` (the exact
targets + filter), `_quote_raw_term`/`_select_raw_query_term` (the exact raw query), the pure
`_compute_harmonization_health` (the exact verdict), and the merge step's `_records_from_item` /
`_health_from_item` (the exact SERVED-corpus selection). The only new code is the dataclass + the
envelope reconstruction that feeds the merge functions.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from apecx_integration.composition.steps.harmonized_bundle_merge_step import (
    _health_from_item,
    _records_from_item,
)
from apecx_integration.composition.steps.harmonized_search_execute_step import (
    _HARMONIZED_FILTER,
    _INDEX_UUIDS,
    _compute_harmonization_health,
    _quote_raw_term,
    _select_raw_query_term,
)

ALL_INDICES: tuple[str, ...] = tuple(_INDEX_UUIDS.keys())

# Eval fetch depth. Default to production's own `_MAX_RECORDS` (10000) — the hard Globus offset ceiling
# (`limit + offset <= 10000`); a single `limit=10000` request returns EVERY match up to it (no offset
# loop, and the scroll API is unimplemented). At this depth BOTH legs are FULLY enumerated for every
# cell whose corpus `total <= 10000`, so the raw∪harm gold pool IS the corpus → recall is TRUE
# full-corpus recall, and the precision K-sample is drawn from the whole served set, not just its head.
# The QUERY construction stays byte-identical to production (raw quoting + harmonized filter reused).
#
# RESIDUAL CAVEAT (surfaced in the findings, now bounded to the tail): a cell whose `total > 10000`
# CANNOT be fully enumerated with the current API, so for those `capped` cells recall is recall@10k and
# precision is over the relevance-ordered head (Globus returns records in relevance order). The
# `capped` flag marks exactly these; everything else is full-corpus.
_DEFAULT_FETCH_LIMIT = 10000


def _fetch(client, index_uuid: str, query_body: dict, limit: int) -> tuple[int, list, bool]:
    """Mirror of `_fetch_records` with a configurable depth. Same projection + capped semantics; `total`
    is the TRUE corpus size (independent of `limit`), so coverage stays exact while the pool is bounded."""
    resp = client.post_search(index_uuid, {**query_body, "limit": limit})
    total = int(resp.data.get("total", 0))
    records = [g["entries"][0]["content"] for g in resp.data.get("gmeta", []) if g.get("entries")]
    return total, records, total > len(records)


@dataclass
class ProbeCell:
    index: str
    term: str
    canonical_iri: str | None
    verdict: str  # harmonization_health verdict for this cell
    raw_total: int
    harm_total: int
    served_from_raw: bool
    raw_records: list = field(default_factory=list)
    harm_records: list = field(default_factory=list)
    served_records: list = field(default_factory=list)
    # ``capped`` = the corpus exceeded the fetch depth (total > fetch_limit). At the default depth
    # (10000 = production's `_MAX_RECORDS`, the hard Globus ceiling) a capped cell is genuinely
    # un-enumerable — recall is recall@10k and precision is over the relevance-ordered head there.
    # A NON-capped cell is fully enumerated → full-corpus recall + whole-corpus precision sample.
    capped: bool = False
    error: str | None = None


def probe_cell(
    client,
    index: str,
    term: str,
    canonical_iri: str | None,
    canonical_label,
    fetch_limit: int = _DEFAULT_FETCH_LIMIT,
) -> ProbeCell:
    """Run the raw + harmonized legs for one (query, index) and derive the served corpus exactly as
    the shipped merge step does. Never raises — a Globus error is captured on ``.error``."""
    uuid = _INDEX_UUIDS[index]
    field_name = _HARMONIZED_FILTER[index]["field"]  # "subjects.valueUri"

    # Raw leg — the shipped step's own term selection THEN phrase-quoting, in that exact order
    # (execute step: `_quote_raw_term(_select_raw_query_term(...))`). Skipping the quote would issue a
    # tokenized-OR query production never sends (`q=West Nile virus` → "West" OR "Nile" OR "virus"),
    # inflating the raw/served corpus for the very degraded cells this eval characterizes.
    raw_q_term, _ = _select_raw_query_term(term, canonical_label)
    raw_q, _ = _quote_raw_term(raw_q_term)
    raw_err = None
    try:
        raw_total, raw_records, raw_capped = _fetch(client, uuid, {"q": raw_q}, fetch_limit)
    except Exception as exc:  # noqa: BLE001 — capture, never raise (batch eval)
        return ProbeCell(index, term, canonical_iri, "errored", 0, 0, False, error=f"raw: {exc}")

    if canonical_iri:
        # Harmonized leg — the SAME match_any subjects.valueUri filter the shipped step builds.
        harm_err = None
        try:
            harm_total, harm_records, harm_capped = _fetch(
                client,
                uuid,
                {
                    "filters": [
                        {"type": "match_any", "field_name": field_name, "values": [canonical_iri]}
                    ]
                },
                fetch_limit,
            )
        except Exception as exc:  # noqa: BLE001
            harm_total, harm_records, harm_capped, harm_err = 0, [], False, str(exc)
        verdict, _ = _compute_harmonization_health(
            raw_total, harm_total, field_name, 1, index, canonical_label, raw_err, harm_err
        )
    else:
        # Resolution MISS: no IRI, so there is NO harmonized leg to run. This is not a query error —
        # mirror production's `_run_miss_envelope` verdict directly. (v1 passed the miss as `harm_err`,
        # tripping `_compute_harmonization_health` into `errored` — 153 miss cells silently mislabeled.)
        harm_total, harm_records, harm_capped = 0, [], False
        verdict = "unharmonized_raw_fallback" if raw_total > 0 else "miss_zero"

    # Reconstruct the shipped per-index envelope so SERVED is chosen by the REAL merge functions.
    item = {
        "envelope_input": {
            "data": {
                "parts": {
                    "raw_query": {"total": raw_total, "records": raw_records},
                    "harmonized_query": {"total": harm_total, "records": harm_records},
                    "harmonization_health": {"verdict": verdict},
                }
            }
        }
    }
    served_records = _records_from_item(item)
    served_verdict = _health_from_item(item) or verdict
    # served came from the raw leg iff the harmonized leg carried nothing (mirrors _records_from_item).
    served_from_raw = not (isinstance(harm_records, list) and len(harm_records) > 0) and bool(
        served_records
    )

    return ProbeCell(
        index=index,
        term=term,
        canonical_iri=canonical_iri,
        verdict=served_verdict,
        raw_total=raw_total,
        harm_total=harm_total,
        served_from_raw=served_from_raw,
        raw_records=raw_records,
        harm_records=harm_records,
        served_records=served_records,
        capped=(raw_capped or harm_capped),
    )
