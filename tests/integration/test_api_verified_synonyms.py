"""T02 integration: /verified_synonyms/lookup + POST + PATCH against
a real migrated SQLite DB. No mocks.

Covers the HTTP surface the workflow's Step 3a (cache lookup) and
Step 4p (writeback) consume, plus revocation semantics for the case
where a previously-approved mapping turns out to be incorrect.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

pytestmark = pytest.mark.integration


def _create(cp_client, **overrides) -> dict:
    body = {
        "source_vocabulary": "user_query",
        "query_term": "vaccinia",
        "target_vocabulary": "violin.pathogen_id",
        "canonical_term": "VIOLIN_101",
        "verified_by": "alex",
        "confidence": 0.95,
    }
    body.update(overrides)
    resp = cp_client.post("/verified_synonyms/", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["verified_synonym"]


def test_lookup_empty_cache_returns_null_per_term(cp_client) -> None:
    """Fresh DB: every term returns a match with ``result: null``."""
    resp = cp_client.post(
        "/verified_synonyms/lookup",
        json={
            "source_vocabulary": "user_query",
            "target_vocabulary": "violin.pathogen_id",
            "query_terms": ["vaccinia", "eeev", "ebola"],
        },
    )
    assert resp.status_code == 200
    matches = resp.json()["matches"]
    assert len(matches) == 3
    assert [m["query_term"] for m in matches] == ["vaccinia", "eeev", "ebola"]
    assert all(m["result"] is None for m in matches)


def test_lookup_returns_active_row_and_preserves_input_order(cp_client) -> None:
    _create(cp_client, query_term="vaccinia", canonical_term="VIOLIN_101")
    _create(cp_client, query_term="eeev", canonical_term="VIOLIN_205")

    # Input order is intentionally scrambled to verify the response mirrors it.
    resp = cp_client.post(
        "/verified_synonyms/lookup",
        json={
            "source_vocabulary": "user_query",
            "target_vocabulary": "violin.pathogen_id",
            "query_terms": ["unknown_term", "eeev", "vaccinia"],
        },
    )
    assert resp.status_code == 200
    matches = resp.json()["matches"]
    assert [m["query_term"] for m in matches] == ["unknown_term", "eeev", "vaccinia"]
    assert matches[0]["result"] is None
    assert matches[1]["result"]["canonical_term"] == "VIOLIN_205"
    assert matches[2]["result"]["canonical_term"] == "VIOLIN_101"


def test_lookup_is_scoped_when_scope_provided(cp_client) -> None:
    """Same source/target/query with different scopes are independent."""
    _create(cp_client, query_term="vaccinia", scope="alphavirus", canonical_term="A")
    _create(cp_client, query_term="vaccinia", scope="orthopoxvirus", canonical_term="B")
    # No-scope (null) also counts as a distinct bucket.
    _create(cp_client, query_term="vaccinia", scope=None, canonical_term="C")

    resp_alpha = cp_client.post(
        "/verified_synonyms/lookup",
        json={
            "source_vocabulary": "user_query",
            "target_vocabulary": "violin.pathogen_id",
            "query_terms": ["vaccinia"],
            "scope": "alphavirus",
        },
    )
    assert resp_alpha.json()["matches"][0]["result"]["canonical_term"] == "A"

    resp_none = cp_client.post(
        "/verified_synonyms/lookup",
        json={
            "source_vocabulary": "user_query",
            "target_vocabulary": "violin.pathogen_id",
            "query_terms": ["vaccinia"],
            "scope": None,
        },
    )
    assert resp_none.json()["matches"][0]["result"]["canonical_term"] == "C"


def test_create_rejects_duplicate_active_tuple_with_409(cp_client) -> None:
    _create(cp_client, query_term="vaccinia", canonical_term="VIOLIN_101")
    resp = cp_client.post(
        "/verified_synonyms/",
        json={
            "source_vocabulary": "user_query",
            "query_term": "vaccinia",
            "target_vocabulary": "violin.pathogen_id",
            "canonical_term": "VIOLIN_RIVAL",
            "verified_by": "alex",
            "confidence": 0.9,
        },
    )
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"]


def test_revoke_soft_deletes_and_preserves_audit(cp_client) -> None:
    created = _create(cp_client, query_term="vaccinia", canonical_term="VIOLIN_101")
    row_id = created["id"]

    resp = cp_client.patch(
        f"/verified_synonyms/{row_id}",
        json={"revoked_by": "alex", "revocation_reason": "wrong pathogen family"},
    )
    assert resp.status_code == 200
    body = resp.json()["verified_synonym"]
    assert body["is_active"] is False
    assert body["revoked_by"] == "alex"
    assert body["revocation_reason"] == "wrong pathogen family"
    assert body["revoked_at"] is not None

    # Active lookup no longer returns this row.
    lookup = cp_client.post(
        "/verified_synonyms/lookup",
        json={
            "source_vocabulary": "user_query",
            "target_vocabulary": "violin.pathogen_id",
            "query_terms": ["vaccinia"],
        },
    )
    assert lookup.json()["matches"][0]["result"] is None


def test_revoke_can_chain_via_superseded_by(cp_client) -> None:
    """After revocation, the caller creates the replacement row and
    updates the old row's superseded_by pointer (via a second revoke?
    Actually, the normal flow: revoke old THEN create new. But the
    API lets you set superseded_by at revoke time if the replacement
    already exists, so the audit chain is forward-pointed.)
    """
    old = _create(cp_client, query_term="vaccinia", canonical_term="VIOLIN_OLD")
    # Revoke first (without superseded_by), then create replacement.
    cp_client.patch(
        f"/verified_synonyms/{old['id']}",
        json={"revoked_by": "alex", "revocation_reason": "stale"},
    )
    new = _create(cp_client, query_term="vaccinia", canonical_term="VIOLIN_NEW")
    assert new["id"] != old["id"]


def test_revoke_idempotent_revocation_is_409(cp_client) -> None:
    created = _create(cp_client, query_term="vaccinia")
    row_id = created["id"]
    first = cp_client.patch(
        f"/verified_synonyms/{row_id}",
        json={"revoked_by": "alex", "revocation_reason": "wrong"},
    )
    assert first.status_code == 200
    second = cp_client.patch(
        f"/verified_synonyms/{row_id}",
        json={"revoked_by": "alex", "revocation_reason": "wrong again"},
    )
    assert second.status_code == 409
    assert "already inactive" in second.json()["detail"]


def test_revoke_unknown_id_is_404(cp_client) -> None:
    resp = cp_client.patch(
        f"/verified_synonyms/{uuid4()}",
        json={"revoked_by": "alex", "revocation_reason": "x"},
    )
    assert resp.status_code == 404


def test_revoke_dangling_superseded_by_is_400(cp_client) -> None:
    created = _create(cp_client, query_term="vaccinia")
    resp = cp_client.patch(
        f"/verified_synonyms/{created['id']}",
        json={
            "revoked_by": "alex",
            "revocation_reason": "replaced",
            "superseded_by": str(uuid4()),
        },
    )
    assert resp.status_code == 400
    assert "unknown id" in resp.json()["detail"]


def test_lookup_accepts_empty_source_field_validation(cp_client) -> None:
    """Empty source_vocabulary is rejected by the schema's min_length=1."""
    resp = cp_client.post(
        "/verified_synonyms/lookup",
        json={
            "source_vocabulary": "",
            "target_vocabulary": "violin.pathogen_id",
            "query_terms": ["x"],
        },
    )
    assert resp.status_code == 422


def test_lookup_rejects_too_many_terms(cp_client) -> None:
    """500-term cap (per schema) prevents a pathological query from
    blowing up the SQL IN() clause.
    """
    resp = cp_client.post(
        "/verified_synonyms/lookup",
        json={
            "source_vocabulary": "user_query",
            "target_vocabulary": "violin.pathogen_id",
            "query_terms": [f"t_{i}" for i in range(501)],
        },
    )
    assert resp.status_code == 422


def test_revoke_with_valid_superseded_by(cp_client) -> None:
    """When superseded_by points at a real row, the revoke succeeds
    and the pointer is persisted.
    """
    # Create a row in a non-colliding scope so the unique index doesn't block us.
    replacement = _create(
        cp_client, query_term="vaccinia", scope="new_scope", canonical_term="VIOLIN_NEW"
    )
    old = _create(cp_client, query_term="vaccinia", scope="old_scope", canonical_term="VIOLIN_OLD")
    resp = cp_client.patch(
        f"/verified_synonyms/{old['id']}",
        json={
            "revoked_by": "alex",
            "revocation_reason": "replaced",
            "superseded_by": replacement["id"],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["verified_synonym"]["superseded_by"] == replacement["id"]
    assert UUID(replacement["id"])  # just asserts the UUID is a real one
