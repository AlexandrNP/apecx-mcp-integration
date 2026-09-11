"""Smoke/unit tests for the pure Globus search-payload builder — NO network.

``_build_search_payload`` is the network-free core of ``client.search``: it turns
(query, limit, offset, advanced, filters) into the exact ``post_search`` payload dict.
Testing it directly asserts the advanced-mode (Lucene) contract and backward
compatibility without ever hitting Globus.
"""

from __future__ import annotations

from apecx_integration.agents.globus_search.client import _build_search_payload


def test_advanced_true_sets_advanced_and_carries_query():
    payload = _build_search_payload(
        "chikungunya AND epitope", limit=10, offset=0, advanced=True, filters=None
    )

    assert payload["advanced"] is True
    assert payload["q"] == "chikungunya AND epitope"
    assert payload["limit"] == 10
    assert payload["offset"] == 0


def test_advanced_false_default_omits_advanced_key_backward_compat():
    payload = _build_search_payload("chikungunya", limit=20, offset=0)

    # Backward compatibility: the original free-text payload carried no advanced/filters keys.
    assert "advanced" not in payload
    assert "filters" not in payload
    assert payload == {"q": "chikungunya", "limit": 20, "offset": 0}


def test_filters_imply_advanced_mode():
    filters = [{"type": "match_any", "field_name": "publisher.name", "values": ["RCSB PDB"]}]
    payload = _build_search_payload("chikv", limit=5, offset=0, filters=filters)

    assert payload["advanced"] is True
    assert payload["filters"] == filters
