"""Probe batch 1 — input validation boundaries.

Each test is one distinct adversarial probe. Pass = no bug in
that area; failure = a real bug worth fixing. The user's stop
criterion is "0 bugs in 100 consecutive different probes."

Categories in this batch:
  - empty / whitespace-only strings on min_length=1 fields
  - extremely long strings (no max_length)
  - non-finite floats (NaN, Infinity)
  - count limits at max_length (off-by-one)
  - unicode / control characters
  - negative numbers on ge=0 fields
"""

from __future__ import annotations

from datetime import UTC, datetime
import math
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text


pytestmark = pytest.mark.integration


def _seed_run(engine: Engine, *, user_id: str = "alex") -> UUID:
    run_id = uuid4()
    now = datetime.now(UTC).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, :uid, 'PENDING', :ts)"
            ),
            {"id": str(run_id), "uid": user_id, "ts": now},
        )
    return run_id


def _seed_run_with_artifact(engine: Engine, tmp_path) -> tuple[UUID, UUID]:
    run_id = uuid4()
    artifact_id = uuid4()
    yaml_path = tmp_path / f"wf_{run_id}.yml"
    yaml_path.write_text("name: x\nsteps: {}\nlinks: {}\n", encoding="utf-8")
    now = datetime.now(UTC).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO run (id, user_id, status, created_at) "
                "VALUES (:id, 'alex', 'RUNNING', :ts)"
            ),
            {"id": str(run_id), "ts": now},
        )
        conn.execute(
            text(
                "INSERT INTO artifact (id, run_id, kind, location, "
                "content_hash, size_bytes, mime_type, created_at) "
                "VALUES (:id, :rid, 'GENERATED_WORKFLOW', :loc, "
                "'sha256-placeholder', 1, 'application/x-yaml', :ts)"
            ),
            {
                "id": str(artifact_id),
                "rid": str(run_id),
                "loc": str(yaml_path),
                "ts": now,
            },
        )
        conn.execute(
            text("UPDATE run SET workflow_config_id = :a WHERE id = :r"),
            {"a": str(artifact_id), "r": str(run_id)},
        )
    return run_id, artifact_id


# --- Probe 31: /workflows/start with empty user_id ---


def test_probe_31_workflows_start_empty_user_id_is_rejected(cp_client: TestClient) -> None:
    resp = cp_client.post(
        "/workflows/start",
        json={"description": "test", "user_id": ""},
    )
    # Pydantic currently doesn't put min_length=1 on user_id. Accept any
    # non-2xx as "rejected." If the route accepts empty user_id and
    # creates a run, that's a real bug — runs would be unattributable.
    assert resp.status_code != 200, (
        "PROBE 31 BUG: /workflows/start accepted empty user_id; runs "
        "with no owner can't be filtered by /runs/list?user_id=. The "
        "schema's user_id should have min_length=1."
    )


# --- Probe 32: /workflows/start with whitespace-only user_id ---


def test_probe_32_workflows_start_whitespace_user_id(cp_client: TestClient) -> None:
    """Whitespace-only user_id passes Pydantic min_length=1 (a space
    is length 1). The route then either accepts (composer
    configured) or returns 503 (composer unconfigured). Either is
    fine — we're checking that the route doesn't 500 on a
    whitespace string. UX issue ("user_id of just spaces") is
    documented but not a fail-fast violation.
    """
    resp = cp_client.post(
        "/workflows/start",
        json={"description": "test", "user_id": "   "},
    )
    assert resp.status_code != 500, (
        f"PROBE 32 BUG: whitespace user_id triggered 500: {resp.text}"
    )


# --- Probe 33: /workflows/start with unicode + emoji description ---


def test_probe_33_workflows_start_unicode_description(cp_client: TestClient) -> None:
    resp = cp_client.post(
        "/workflows/start",
        json={
            "description": "测试 émoji 🧬 café",
            "user_id": "alex",
        },
    )
    # Composer might be unconfigured (503) which is fine. Just verify
    # the route doesn't 500 on unicode input.
    assert resp.status_code in (200, 503), (
        f"PROBE 33 BUG: unicode description triggered {resp.status_code}: {resp.text}"
    )


# --- Probe 34: /workflows/start with description > 100K chars ---


def test_probe_34_workflows_start_huge_description(cp_client: TestClient) -> None:
    resp = cp_client.post(
        "/workflows/start",
        json={
            "description": "x" * 100_000,
            "user_id": "alex",
        },
    )
    # 100K chars is unusually large but no max_length on the schema.
    # Acceptable: 200 (composer unconfigured-503), or 4xx if a future
    # schema adds max_length. 500 = bug (ungraceful).
    assert resp.status_code != 500, (
        f"PROBE 34 BUG: 100K-char description caused 500: {resp.text}"
    )


# --- Probe 35: /verified_synonyms create with negative confidence ---


def test_probe_35_verified_synonyms_negative_confidence(cp_client: TestClient) -> None:
    resp = cp_client.post(
        "/verified_synonyms/",
        json={
            "source_vocabulary": "violin",
            "query_term": "x",
            "target_vocabulary": "bvbrc",
            "canonical_term": "X",
            "verified_by": "alex",
            "confidence": -0.5,
        },
    )
    assert resp.status_code == 422, (
        f"PROBE 35 BUG: confidence=-0.5 not rejected by Pydantic ge=0.0; "
        f"got {resp.status_code}: {resp.text}"
    )


# --- Probe 36: /verified_synonyms create with confidence > 1 ---


def test_probe_36_verified_synonyms_confidence_above_one(cp_client: TestClient) -> None:
    resp = cp_client.post(
        "/verified_synonyms/",
        json={
            "source_vocabulary": "violin",
            "query_term": "x",
            "target_vocabulary": "bvbrc",
            "canonical_term": "X",
            "verified_by": "alex",
            "confidence": 1.5,
        },
    )
    assert resp.status_code == 422, (
        f"PROBE 36 BUG: confidence=1.5 not rejected by Pydantic le=1.0"
    )


# --- Probe 37: /hpc/confirm with NaN ---


def test_probe_37_hpc_confirm_nan(cp_engine: Engine, cp_client: TestClient, tmp_path) -> None:
    run_id, _ = _seed_run_with_artifact(cp_engine, tmp_path)
    # Insert an estimate so the route doesn't 422 on "no estimate"
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO allocation_estimate (id, run_id, "
                "estimated_core_hours, estimated_wall_time_seconds, "
                "endpoint, user_confirmed, created_at) VALUES "
                "(:id, :rid, 5.0, 18000, 'polaris', 0, :ts)"
            ),
            {
                "id": str(uuid4()),
                "rid": str(run_id),
                "ts": datetime.now(UTC).isoformat(),
            },
        )
    # Pydantic + httpx tries to JSON-encode NaN. Standard JSON forbids
    # NaN, but Python's json module emits it as `NaN` (non-standard)
    # by default. Send raw to see what the route does.
    import httpx

    resp = cp_client.post(
        "/hpc/confirm",
        content=b'{"run_id": "%s", "confirmed_core_hours": NaN}' % str(run_id).encode(),
        headers={"content-type": "application/json"},
    )
    # NaN should be rejected — either Pydantic catches it as not-a-real-
    # number, or the JSON parser rejects non-standard NaN. Either way,
    # 4xx not 500/200.
    assert resp.status_code in (400, 422), (
        f"PROBE 37 BUG: NaN confirmed_core_hours got {resp.status_code}: "
        f"{resp.text}. Should reject as invalid number."
    )


# --- Probe 38: /hpc/confirm with Infinity ---


def test_probe_38_hpc_confirm_infinity(
    cp_engine: Engine, cp_client: TestClient, tmp_path
) -> None:
    run_id, _ = _seed_run_with_artifact(cp_engine, tmp_path)
    with cp_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO allocation_estimate (id, run_id, "
                "estimated_core_hours, estimated_wall_time_seconds, "
                "endpoint, user_confirmed, created_at) VALUES "
                "(:id, :rid, 5.0, 18000, 'polaris', 0, :ts)"
            ),
            {
                "id": str(uuid4()),
                "rid": str(run_id),
                "ts": datetime.now(UTC).isoformat(),
            },
        )
    resp = cp_client.post(
        "/hpc/confirm",
        content=b'{"run_id": "%s", "confirmed_core_hours": Infinity}' % str(run_id).encode(),
        headers={"content-type": "application/json"},
    )
    # Infinity passing means user "confirms unlimited core-hours" which
    # is dangerous in any HPC context. Should reject.
    assert resp.status_code in (400, 422), (
        f"PROBE 38 BUG: Infinity confirmed_core_hours accepted as "
        f"{resp.status_code}: {resp.text}. Allows unbounded allocation."
    )


# --- Probe 39: /verified_synonyms/lookup with max_length=500 (boundary) ---


def test_probe_39_verified_synonyms_lookup_500_terms(cp_client: TestClient) -> None:
    resp = cp_client.post(
        "/verified_synonyms/lookup",
        json={
            "source_vocabulary": "violin",
            "target_vocabulary": "bvbrc",
            "query_terms": [f"t{i}" for i in range(500)],
        },
    )
    assert resp.status_code == 200, (
        f"PROBE 39 BUG: 500 terms (max_length boundary) rejected: "
        f"{resp.status_code} {resp.text}"
    )


# --- Probe 40: /verified_synonyms/lookup with 501 terms (over limit) ---


def test_probe_40_verified_synonyms_lookup_501_terms(cp_client: TestClient) -> None:
    resp = cp_client.post(
        "/verified_synonyms/lookup",
        json={
            "source_vocabulary": "violin",
            "target_vocabulary": "bvbrc",
            "query_terms": [f"t{i}" for i in range(501)],
        },
    )
    assert resp.status_code == 422, (
        f"PROBE 40 BUG: 501 terms NOT rejected by max_length=500"
    )


# --- Probe 41: /runs/list with limit=0 (Pydantic ge=1) ---


def test_probe_41_runs_list_limit_zero_rejected(cp_client: TestClient) -> None:
    resp = cp_client.post(
        "/runs/list",
        json={"user_id": "alex", "limit": 0},
    )
    assert resp.status_code == 422


# --- Probe 42: /runs/list with limit > 500 (Pydantic le=500) ---


def test_probe_42_runs_list_limit_too_high_rejected(cp_client: TestClient) -> None:
    resp = cp_client.post(
        "/runs/list",
        json={"user_id": "alex", "limit": 1000},
    )
    assert resp.status_code == 422


# --- Probe 43: /runs/list user_id with newline injection ---


def test_probe_43_runs_list_newline_user_id(cp_client: TestClient) -> None:
    resp = cp_client.post(
        "/runs/list",
        json={"user_id": "alex\nadmin", "limit": 10},
    )
    # Should accept the literal string (no SQL injection because
    # parameter binding is safe). Returns empty list since no runs
    # match that user_id.
    assert resp.status_code == 200, (
        f"PROBE 43 BUG: newline in user_id got {resp.status_code}; "
        "parameter-bound queries should handle any string safely"
    )


# --- Probe 44: /verified_synonyms create with SQL-special chars ---


def test_probe_44_verified_synonyms_create_quote_injection(cp_client: TestClient) -> None:
    resp = cp_client.post(
        "/verified_synonyms/",
        json={
            "source_vocabulary": "violin",
            "query_term": "'; DROP TABLE run; --",
            "target_vocabulary": "bvbrc",
            "canonical_term": "X' UNION SELECT * FROM secrets --",
            "verified_by": "alex",
            "confidence": 1.0,
            "scope": "test-injection",
        },
    )
    assert resp.status_code == 200, (
        f"PROBE 44 BUG: SQL-quote string in fields rejected: "
        f"{resp.status_code} (should be safely parameter-bound)"
    )


# --- Probe 45: /runs/status for non-existent run_id ---


def test_probe_45_runs_status_nonexistent(cp_client: TestClient) -> None:
    resp = cp_client.post(
        "/runs/status",
        json={"run_id": str(uuid4())},
    )
    assert resp.status_code == 404


# --- Probe 46: /runs/artifact for non-existent artifact_id ---


def test_probe_46_runs_artifact_nonexistent(cp_client: TestClient) -> None:
    resp = cp_client.post(
        "/runs/artifact",
        json={"artifact_id": str(uuid4())},
    )
    assert resp.status_code == 404


# --- Probe 47: /verified_synonyms revoke nonexistent ---


def test_probe_47_verified_synonyms_revoke_nonexistent(cp_client: TestClient) -> None:
    resp = cp_client.patch(
        f"/verified_synonyms/{uuid4()}",
        json={
            "revoked_by": "alex",
            "revocation_reason": "test",
        },
    )
    assert resp.status_code == 404


# --- Probe 48: /approvals/{id} GET nonexistent ---


def test_probe_48_approvals_get_nonexistent(cp_client: TestClient) -> None:
    resp = cp_client.get(f"/approvals/{uuid4()}")
    assert resp.status_code == 404


# --- Probe 49: /approvals/approve nonexistent ---


def test_probe_49_approvals_approve_nonexistent(cp_client: TestClient) -> None:
    resp = cp_client.post(
        "/approvals/approve",
        json={"approval_id": str(uuid4()), "decided_by": "alex"},
    )
    assert resp.status_code == 404


# --- Probe 50: /hpc/estimate for nonexistent run ---


def test_probe_50_hpc_estimate_nonexistent_run(cp_client: TestClient) -> None:
    resp = cp_client.post(
        "/hpc/estimate",
        json={"run_id": str(uuid4())},
    )
    assert resp.status_code == 404
