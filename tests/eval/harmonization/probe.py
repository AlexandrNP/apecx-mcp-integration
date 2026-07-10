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

# Eval fetch depth for the recall pool. Production's `_fetch_records` hard-pins the 10k Globus offset
# ceiling; pulling 10k records × 2 legs × ~1260 cells is network-impractical (hours). Since precision
# is judged on a K-sample of the served set and coverage uses `total` (returned free of fetch depth),
# the deep fetch only sets recall POOL DEPTH — so we bound it and report recall as recall@FETCH_LIMIT.
# The QUERY construction stays byte-identical to production (raw quoting + harmonized filter reused);
# only the eval's own pool depth changes.
#
# PRECISION-OPTIMISM CAVEAT (must be surfaced in the findings): for a cell whose true served corpus
# EXCEEDS fetch_limit, the K-sample is drawn only from the first `fetch_limit` records, which Globus
# returns in RELEVANCE order. Relevant records cluster at the head, so precision on those high-volume
# cells is directionally OPTIMISTIC (later pages carry more incidental/multi-subject matches we never
# see). This is bounded — most cells have total < fetch_limit and are unaffected — but the direction is
# not neutral. A high-precision headline on a heavy index must be read as "over the relevance head".
_DEFAULT_FETCH_LIMIT = 1500


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
    # NOTE: under the bounded eval fetch, ``capped`` means "the pool was bounded at fetch_limit"
    # (total > fetch_limit), NOT production's "hit the 10k offset ceiling". A capped cell is where the
    # precision-optimism caveat applies (see _DEFAULT_FETCH_LIMIT).
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
    raw_err = harm_err = None
    try:
        raw_total, raw_records, raw_capped = _fetch(client, uuid, {"q": raw_q}, fetch_limit)
    except Exception as exc:  # noqa: BLE001 — capture, never raise (batch eval)
        return ProbeCell(index, term, canonical_iri, "errored", 0, 0, False, error=f"raw: {exc}")

    # Harmonized leg — the SAME match_any subjects.valueUri filter the shipped step builds.
    if canonical_iri:
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
    else:
        harm_total, harm_records, harm_capped = 0, [], False
        harm_err = "no canonical IRI (miss)"

    verdict, _ = _compute_harmonization_health(
        raw_total,
        harm_total,
        field_name,
        1 if canonical_iri else 0,
        index,
        canonical_label,
        raw_err,
        harm_err,
    )

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
