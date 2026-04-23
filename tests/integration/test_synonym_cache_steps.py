"""T02 integration: SynonymCacheLookupStep + VerifiedSynonymWritebackStep.

Drives the two nanobrain steps against a live FastAPI Control Plane
(in-process via httpx.ASGITransport) backed by migrated SQLite.
Same pattern as test_approval_step_integration.py.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import FastAPI

from apecx_integration.composition.steps.synonym_cache import (
    SynonymCacheLookupStep,
    VerifiedSynonymWritebackStep,
)

pytestmark = pytest.mark.integration


def _build_lookup_step(
    app: FastAPI,
    tmp_path: Path,
    *,
    source_vocabulary: str = "user_query",
    target_vocabulary: str = "violin.pathogen_id",
    scope: str | None = None,
) -> SynonymCacheLookupStep:
    config = {
        "name": f"lookup_{uuid.uuid4().hex[:8]}",
        "description": "test",
        "source_vocabulary": source_vocabulary,
        "target_vocabulary": target_vocabulary,
        "scope": scope,
        "control_plane": {
            "base_url": "http://testserver",
            "request_timeout_seconds": 5.0,
        },
    }
    config_path = tmp_path / f"lookup_{uuid.uuid4().hex[:8]}.yml"
    config_path.write_text(yaml.safe_dump(config))
    step = SynonymCacheLookupStep.from_config(str(config_path))
    step._http_client_factory = lambda: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        timeout=5.0,
    )
    return step


def _build_writeback_step(
    app: FastAPI,
    tmp_path: Path,
    *,
    source_vocabulary: str = "user_query",
    target_vocabulary: str = "violin.pathogen_id",
    scope: str | None = None,
    verified_by: str = "alex",
) -> VerifiedSynonymWritebackStep:
    config = {
        "name": f"writeback_{uuid.uuid4().hex[:8]}",
        "description": "test",
        "source_vocabulary": source_vocabulary,
        "target_vocabulary": target_vocabulary,
        "scope": scope,
        "verified_by": verified_by,
        "control_plane": {
            "base_url": "http://testserver",
            "request_timeout_seconds": 5.0,
        },
    }
    config_path = tmp_path / f"writeback_{uuid.uuid4().hex[:8]}.yml"
    config_path.write_text(yaml.safe_dump(config))
    step = VerifiedSynonymWritebackStep.from_config(str(config_path))
    step._http_client_factory = lambda: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        timeout=5.0,
    )
    return step


async def test_lookup_step_partitions_cached_vs_novel(cp_client, cp_engine, tmp_path):
    from apecx_integration.control_plane.app import create_app

    app = create_app(engine=cp_engine)

    # Seed the cache directly via the HTTP endpoint we just built.
    cp_client.post(
        "/verified_synonyms/",
        json={
            "source_vocabulary": "user_query",
            "query_term": "vaccinia",
            "target_vocabulary": "violin.pathogen_id",
            "canonical_term": "VIOLIN_101",
            "verified_by": "alex",
            "confidence": 0.95,
        },
    )

    step = _build_lookup_step(app, tmp_path)
    result = await step.process({"query_terms": ["vaccinia", "eeev", "ebola"]})

    assert result["cached_mappings"] == {"vaccinia": "VIOLIN_101"}
    assert sorted(result["novel_terms"]) == ["ebola", "eeev"]


async def test_lookup_step_empty_input_is_no_op(cp_client, cp_engine, tmp_path):
    from apecx_integration.control_plane.app import create_app

    app = create_app(engine=cp_engine)
    step = _build_lookup_step(app, tmp_path)
    result = await step.process({"query_terms": []})
    assert result == {"cached_mappings": {}, "novel_terms": []}


async def test_lookup_step_rejects_bad_input_shape(cp_client, cp_engine, tmp_path):
    from apecx_integration.control_plane.app import create_app

    app = create_app(engine=cp_engine)
    step = _build_lookup_step(app, tmp_path)
    with pytest.raises(ValueError, match="list\\[str\\]"):
        await step.process({"query_terms": "not-a-list"})


async def test_writeback_step_persists_approved_mappings(cp_client, cp_engine, tmp_path):
    from apecx_integration.control_plane.app import create_app

    app = create_app(engine=cp_engine)
    step = _build_writeback_step(app, tmp_path)

    result = await step.process(
        {
            "approved_mappings": [
                {"query_term": "eeev", "canonical_term": "VIOLIN_205", "confidence": 0.9},
                {"query_term": "vee", "canonical_term": "VIOLIN_210", "confidence": 0.85},
            ],
        }
    )
    assert len(result["written"]) == 2
    assert result["already_existed"] == []

    # Verify the rows landed and lookup sees them.
    lookup = cp_client.post(
        "/verified_synonyms/lookup",
        json={
            "source_vocabulary": "user_query",
            "target_vocabulary": "violin.pathogen_id",
            "query_terms": ["eeev", "vee"],
        },
    )
    matches = lookup.json()["matches"]
    assert matches[0]["result"]["canonical_term"] == "VIOLIN_205"
    assert matches[1]["result"]["canonical_term"] == "VIOLIN_210"


async def test_writeback_step_tolerates_409_as_already_existed(cp_client, cp_engine, tmp_path):
    """Race condition: another run wrote the same mapping first. The
    writeback step reports this via ``already_existed``, not as an
    exception — per the "approval-race collision is not an error"
    design note.
    """
    from apecx_integration.control_plane.app import create_app

    app = create_app(engine=cp_engine)

    # Pre-seed: another run already recorded this mapping.
    cp_client.post(
        "/verified_synonyms/",
        json={
            "source_vocabulary": "user_query",
            "query_term": "eeev",
            "target_vocabulary": "violin.pathogen_id",
            "canonical_term": "VIOLIN_EXISTING",
            "verified_by": "alice",
            "confidence": 0.95,
        },
    )

    step = _build_writeback_step(app, tmp_path)
    result = await step.process(
        {
            "approved_mappings": [
                {"query_term": "eeev", "canonical_term": "VIOLIN_COMPETING", "confidence": 0.9},
                {"query_term": "vee", "canonical_term": "VIOLIN_NEW", "confidence": 0.9},
            ],
        }
    )
    assert result["already_existed"] == ["eeev"]
    assert len(result["written"]) == 1  # only 'vee' got through

    # Confirm the original mapping won (first writer, not last).
    lookup = cp_client.post(
        "/verified_synonyms/lookup",
        json={
            "source_vocabulary": "user_query",
            "target_vocabulary": "violin.pathogen_id",
            "query_terms": ["eeev"],
        },
    )
    assert lookup.json()["matches"][0]["result"]["canonical_term"] == "VIOLIN_EXISTING"


async def test_writeback_step_accepts_llm_proposals_passthrough(cp_client, cp_engine, tmp_path):
    """T04 contract: VerifiedSynonymWritebackStep must accept the
    ``llm_proposals`` shape that ApprovalStep emits when it passes the
    Step 3c output through unmodified. This avoids the need for a
    TransformLink between Step 4 and Step 4p.

    Field rename per ``_coerce_input``:
      query_entity → query_term
      synonym → canonical_term
      score → confidence
    """
    from apecx_integration.control_plane.app import create_app

    app = create_app(engine=cp_engine)
    step = _build_writeback_step(app, tmp_path)

    result = await step.process(
        {
            "llm_proposals": [
                {"query_entity": "eeev", "synonym": "VIOLIN_205", "score": 0.92},
                {"query_entity": "vee", "synonym": "VIOLIN_210", "score": 0.88},
            ],
        }
    )
    assert len(result["written"]) == 2
    assert result["already_existed"] == []

    # Verify the rows landed under the renamed fields.
    lookup = cp_client.post(
        "/verified_synonyms/lookup",
        json={
            "source_vocabulary": "user_query",
            "target_vocabulary": "violin.pathogen_id",
            "query_terms": ["eeev", "vee"],
        },
    )
    matches = lookup.json()["matches"]
    assert matches[0]["result"]["canonical_term"] == "VIOLIN_205"
    assert matches[1]["result"]["canonical_term"] == "VIOLIN_210"


async def test_writeback_step_rejects_input_with_neither_shape(cp_client, cp_engine, tmp_path):
    """If neither ``approved_mappings`` nor ``llm_proposals`` is present,
    raise a clear ValueError instead of silently doing nothing.
    """
    from apecx_integration.control_plane.app import create_app

    app = create_app(engine=cp_engine)
    step = _build_writeback_step(app, tmp_path)

    with pytest.raises(ValueError, match="approved_mappings.*llm_proposals"):
        await step.process({"some_other_key": []})


async def test_lookup_and_writeback_round_trip_through_the_workflow_shape(
    cp_client, cp_engine, tmp_path
):
    """End-to-end dance: lookup empty cache -> novel terms -> writeback
    -> lookup again -> cache hit. Exercises the Step 3a + 4p seam that
    the real workflow will walk.
    """
    from apecx_integration.control_plane.app import create_app

    app = create_app(engine=cp_engine)
    lookup = _build_lookup_step(app, tmp_path)
    writeback = _build_writeback_step(app, tmp_path)

    r1 = await lookup.process({"query_terms": ["eeev", "vee"]})
    assert r1["cached_mappings"] == {}
    assert sorted(r1["novel_terms"]) == ["eeev", "vee"]

    # Simulate: composer+LLM+human produced these approvals.
    approved = [
        {"query_term": "eeev", "canonical_term": "VIOLIN_205", "confidence": 0.95},
        {"query_term": "vee", "canonical_term": "VIOLIN_210", "confidence": 0.92},
    ]
    await writeback.process({"approved_mappings": approved})

    # Second run: full cache hit.
    r2 = await lookup.process({"query_terms": ["eeev", "vee"]})
    assert r2["cached_mappings"] == {"eeev": "VIOLIN_205", "vee": "VIOLIN_210"}
    assert r2["novel_terms"] == []
