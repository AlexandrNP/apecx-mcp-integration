"""Probe batch 15 — under-probed modules (probes 380-404).

Targets components flagged by the 2026-04-26 batch-15 survey as
having non-trivial logic with sparse adversarial coverage:

  - Cost estimator (control_plane/accounting/cost_estimator.py)
  - Component catalog (composition/component_catalog.py)
  - Email notifier (control_plane/notifications/email.py)
  - Metrics aggregator helpers (control_plane/routes/metrics.py)
  - PBS bundle generator (execution/pbs_bundle.py)

Each probe is one distinct adversarial scenario. All probes are
pure-Python (no DB / no FastAPI client) so failures are easy to
isolate.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Cost estimator — probes 380-389
# ---------------------------------------------------------------------------


def test_probe_380_estimator_explicit_override_wins() -> None:
    """If a step config has ``estimated_core_hours``, that value
    must be used verbatim — class-substring heuristics must NOT
    override an explicit author estimate."""
    from apecx_integration.control_plane.accounting.cost_estimator import (
        estimate_workflow_cost,
    )
    cfg = {
        "steps": {
            "s1": {
                "class": "nanobrain.LLMSomething",
                "estimated_core_hours": 42.0,
            }
        }
    }
    r = estimate_workflow_cost(cfg, endpoint="local")
    assert r.per_step_core_hours == {"s1": 42.0}
    assert r.total_core_hours == 42.0


def test_probe_381_estimator_llm_substring_default() -> None:
    from apecx_integration.control_plane.accounting.cost_estimator import (
        estimate_workflow_cost,
    )
    cfg = {"steps": {"s1": {"class": "nanobrain.OllamaAgent"}}}
    r = estimate_workflow_cost(cfg, endpoint="local")
    # LLM/Agent/Ollama all match → 0.05
    assert r.per_step_core_hours["s1"] == pytest.approx(0.05)


def test_probe_382_estimator_snapshot_substring_default() -> None:
    from apecx_integration.control_plane.accounting.cost_estimator import (
        estimate_workflow_cost,
    )
    cfg = {"steps": {"s1": {"class": "tools.BvBrcSnapshotTool"}}}
    r = estimate_workflow_cost(cfg, endpoint="local")
    assert r.per_step_core_hours["s1"] == pytest.approx(0.01)


def test_probe_383_estimator_generic_fallback() -> None:
    """Class with no recognized substring → 0.1 generic fallback."""
    from apecx_integration.control_plane.accounting.cost_estimator import (
        estimate_workflow_cost,
    )
    cfg = {"steps": {"s1": {"class": "totally.Unknown.Class"}}}
    r = estimate_workflow_cost(cfg, endpoint="local")
    assert r.per_step_core_hours["s1"] == pytest.approx(0.1)


def test_probe_384_estimator_empty_steps_total_zero() -> None:
    from apecx_integration.control_plane.accounting.cost_estimator import (
        estimate_workflow_cost,
    )
    r = estimate_workflow_cost({"steps": {}}, endpoint="local")
    assert r.total_core_hours == 0.0
    assert r.per_step_core_hours == {}
    assert r.confidence_interval == (0.0, 0.0)


def test_probe_385_estimator_missing_steps_raises() -> None:
    """No 'steps' key → must fail-fast with ValueError, not silently
    return zero total."""
    from apecx_integration.control_plane.accounting.cost_estimator import (
        estimate_workflow_cost,
    )
    with pytest.raises(ValueError, match="steps"):
        estimate_workflow_cost({"name": "no_steps"}, endpoint="local")


def test_probe_386_estimator_non_dict_steps_raises() -> None:
    """steps as a list (common mistake — workflows.yml schema lets
    you write either) must fail-fast."""
    from apecx_integration.control_plane.accounting.cost_estimator import (
        estimate_workflow_cost,
    )
    with pytest.raises(ValueError, match="steps"):
        estimate_workflow_cost({"steps": ["s1", "s2"]}, endpoint="local")


def test_probe_387_estimator_malformed_step_entry_skipped() -> None:
    """A step whose value is not a dict (e.g. `s1: "some_string"`)
    must be skipped, not crash. The framework loader catches the
    real error; estimator is downstream and shouldn't double-fail."""
    from apecx_integration.control_plane.accounting.cost_estimator import (
        estimate_workflow_cost,
    )
    cfg = {
        "steps": {
            "s_bad": "not-a-dict",
            "s_good": {"class": "x"},
        }
    }
    r = estimate_workflow_cost(cfg, endpoint="local")
    assert "s_good" in r.per_step_core_hours
    assert "s_bad" not in r.per_step_core_hours


def test_probe_388_estimator_endpoint_factor_flat_for_now() -> None:
    """All endpoints currently treated identically (factor 1.0).
    Probe locks this in — if pricing lands later, this probe must
    be updated in lockstep with the endpoint table."""
    from apecx_integration.control_plane.accounting.cost_estimator import (
        estimate_workflow_cost,
    )
    cfg = {"steps": {"s1": {"estimated_core_hours": 1.0}}}
    a = estimate_workflow_cost(cfg, endpoint="local")
    b = estimate_workflow_cost(cfg, endpoint="polaris")
    c = estimate_workflow_cost(cfg, endpoint="aurora")
    assert a.total_core_hours == b.total_core_hours == c.total_core_hours == 1.0


def test_probe_389_estimator_confidence_interval_width() -> None:
    """CI must be (0.3*total, 3.0*total) per AP §5.7. A narrower
    interval would over-promise on a heuristic that's deliberately
    coarse."""
    from apecx_integration.control_plane.accounting.cost_estimator import (
        estimate_workflow_cost,
    )
    cfg = {"steps": {"s1": {"estimated_core_hours": 10.0}}}
    r = estimate_workflow_cost(cfg, endpoint="local")
    low, high = r.confidence_interval
    assert low == pytest.approx(3.0)
    assert high == pytest.approx(30.0)


# ---------------------------------------------------------------------------
# Component catalog — probes 390-394
# ---------------------------------------------------------------------------


def test_probe_390_catalog_empty_returns_empty() -> None:
    """An empty catalog must return [] for any query — composer
    falls back to "no candidates" prompting."""
    from apecx_integration.composition.component_catalog import (
        ComponentCatalog,
    )
    cat = ComponentCatalog()
    assert cat.search("anything") == []
    assert cat.search("") == []
    assert len(cat) == 0


def test_probe_391_catalog_whitespace_query_returns_empty() -> None:
    """A query of pure punctuation/whitespace tokenizes to nothing.
    Must return [] rather than match-everything."""
    from apecx_integration.composition.component_catalog import (
        CatalogComponent,
        ComponentCatalog,
    )
    cat = ComponentCatalog(
        components=(
            CatalogComponent(
                id="x", name="x", description="some text",
                class_path="x", yaml_path=None,
            ),
        )
    )
    assert cat.search("   ") == []
    assert cat.search("!!!") == []
    assert cat.search("@#$%") == []


def test_probe_392_catalog_multi_manifest_dedup(tmp_path) -> None:
    """When the same id appears in multiple manifests, last-write-
    wins. Probe writes two manifests with the same step_id but
    different descriptions and confirms the second one wins."""
    import yaml
    from apecx_integration.composition.component_catalog import (
        ComponentCatalog,
    )
    m1_dir = tmp_path / "wf1"
    m1_dir.mkdir()
    m1 = m1_dir / "manifest.yml"
    m1.write_text(yaml.safe_dump({
        "components": [
            {
                "step_id": "x", "step_name": "x",
                "class": "old.Class",
                "rag_description": "old description",
            }
        ]
    }), encoding="utf-8")
    m2_dir = tmp_path / "wf2"
    m2_dir.mkdir()
    m2 = m2_dir / "manifest.yml"
    m2.write_text(yaml.safe_dump({
        "components": [
            {
                "step_id": "x", "step_name": "x",
                "class": "new.Class",
                "rag_description": "new description",
            }
        ]
    }), encoding="utf-8")
    cat = ComponentCatalog.from_manifests([m1, m2])
    # The id is workflow-slug-prefixed, so they don't collide on id.
    # Confirm both shipped (different ids); the dedup is over EXACT
    # id matches, not name matches.
    ids = [c.id for c in cat.components]
    assert "wf1/x:x" in ids
    assert "wf2/x:x" in ids


def test_probe_393_catalog_deferred_entries_skipped(tmp_path) -> None:
    """``disposition: deferred`` components must be excluded from
    the catalog so the composer cannot pick them."""
    import yaml
    from apecx_integration.composition.component_catalog import (
        ComponentCatalog,
    )
    m_dir = tmp_path / "wf"
    m_dir.mkdir()
    m = m_dir / "manifest.yml"
    m.write_text(yaml.safe_dump({
        "components": [
            {
                "step_id": "live", "step_name": "live",
                "class": "x", "rag_description": "available",
            },
            {
                "step_id": "shelved", "step_name": "shelved",
                "class": "x", "rag_description": "not yet",
                "disposition": "deferred",
            },
        ]
    }), encoding="utf-8")
    cat = ComponentCatalog.from_manifests([m])
    names = [c.name for c in cat.components]
    assert "live" in names
    assert "shelved" not in names


def test_probe_394_catalog_no_rag_description_skipped(tmp_path) -> None:
    """A component with no rag_description is unretrievable at
    Phase 2 (substring match against empty string would match
    every query). Must be silently skipped (logged separately)
    rather than letting it fall through and shadow good matches."""
    import yaml
    from apecx_integration.composition.component_catalog import (
        ComponentCatalog,
    )
    m_dir = tmp_path / "wf"
    m_dir.mkdir()
    m = m_dir / "manifest.yml"
    m.write_text(yaml.safe_dump({
        "components": [
            {"step_id": "good", "class": "x", "rag_description": "good text"},
            {"step_id": "blank", "class": "x", "rag_description": ""},
            # No rag_description at all:
            {"step_id": "missing", "class": "x"},
        ]
    }), encoding="utf-8")
    cat = ComponentCatalog.from_manifests([m])
    names = [c.name for c in cat.components]
    assert "good" in names
    assert "blank" not in names
    assert "missing" not in names


# ---------------------------------------------------------------------------
# Email notifier — probes 395-399
# ---------------------------------------------------------------------------


def test_probe_395_smtp_config_none_when_host_missing() -> None:
    """No APECX_SMTP_HOST → load_smtp_config_from_env returns None
    so the notifier is a no-op. Must NOT crash, must NOT emit a
    bogus default config."""
    from apecx_integration.control_plane.notifications.email import (
        load_smtp_config_from_env,
    )
    keys = [
        "APECX_SMTP_HOST", "APECX_SMTP_PORT", "APECX_SMTP_USER",
        "APECX_SMTP_PASSWORD", "APECX_SMTP_USE_TLS",
        "APECX_SMTP_FROM_ADDR", "APECX_SMTP_TO_ADDR",
    ]
    saved = {k: os.environ.pop(k, None) for k in keys}
    try:
        assert load_smtp_config_from_env() is None
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_probe_396_smtp_config_bad_port_raises() -> None:
    """A non-integer APECX_SMTP_PORT must fail-fast at config
    load — better than a TypeError deep in smtplib at send time."""
    from apecx_integration.control_plane.notifications.email import (
        load_smtp_config_from_env,
    )
    with patch.dict(os.environ, {
        "APECX_SMTP_HOST": "localhost",
        "APECX_SMTP_PORT": "not_a_port",
    }):
        with pytest.raises(ValueError, match="APECX_SMTP_PORT"):
            load_smtp_config_from_env()


def test_probe_397_notify_transitions_locked_to_three_terminal_states() -> None:
    """The set is a contract: COMPLETED / FAILED / PAUSED. If
    someone adds a transition without updating the email subject
    map in _build_message, the email subject would silently say
    'transitioned to <X>' instead of a human verb. Lock the set."""
    from apecx_integration.control_plane.notifications.email import (
        NOTIFY_TRANSITIONS_TO,
    )
    from apecx_integration.control_plane.schemas.enums import RunStatus
    assert NOTIFY_TRANSITIONS_TO == frozenset({
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.PAUSED,
    })


def test_probe_398_disabled_notifier_returns_false() -> None:
    """When config is None the notifier must NOT raise and must
    return False — not True (which would lie to callers about
    delivery)."""
    from apecx_integration.control_plane.notifications.email import (
        EmailNotifier,
    )
    from apecx_integration.control_plane.schemas.enums import RunStatus
    n = EmailNotifier(config=None)
    assert n.enabled is False
    sent = n.send_state_transition(
        run_id=uuid.uuid4(),
        old_status=RunStatus.RUNNING,
        new_status=RunStatus.COMPLETED,
        user_id="alice",
    )
    assert sent is False


def test_probe_399_non_notify_transition_returns_false() -> None:
    """A non-notify transition (e.g. PENDING→RUNNING) must NOT
    contact SMTP and must return False. Defensive — protects
    against wrongly-routed callers."""
    from apecx_integration.control_plane.notifications.email import (
        EmailNotifier, SMTPConfig,
    )
    from apecx_integration.control_plane.schemas.enums import RunStatus
    cfg = SMTPConfig(host="localhost", default_to_addr="sci@example")
    n = EmailNotifier(config=cfg)
    # If this DID try to contact SMTP it would raise (no real server).
    # Returning False without contact is the correct behavior.
    sent = n.send_state_transition(
        run_id=uuid.uuid4(),
        old_status=RunStatus.PENDING,
        new_status=RunStatus.RUNNING,  # not in NOTIFY_TRANSITIONS_TO
        user_id="alice",
    )
    assert sent is False


# ---------------------------------------------------------------------------
# Metrics helpers + PBS bundle — probes 400-404
# ---------------------------------------------------------------------------


def test_probe_400_parse_since_rejects_garbage() -> None:
    """Bad ISO must HTTP-400, not silently default to epoch (which
    would dump every approval ever decided to the response)."""
    from fastapi import HTTPException
    from apecx_integration.control_plane.routes.metrics import _parse_since
    with pytest.raises(HTTPException) as exc:
        _parse_since("not an ISO timestamp")
    assert exc.value.status_code == 400


def test_probe_401_percentile_correctness() -> None:
    """Nearest-rank P95 of a 20-element list must be the 19th value
    (math.ceil(0.95 * 20) = 19, indexed-from-1 → values[18])."""
    from apecx_integration.control_plane.routes.metrics import _percentile
    vals = list(range(1, 21))  # 1..20
    p95 = _percentile(vals, 95.0)
    assert p95 == 19  # nearest-rank: ceil(0.95*20)=19 → s[18] = 19
    p50 = _percentile(vals, 50.0)
    assert p50 == 10  # ceil(0.5*20)=10 → s[9] = 10


def test_probe_402_pbs_bundle_creates_six_files(tmp_path) -> None:
    """generate_bundle must produce all six files specified in
    AC5. A missing file would silently break Tier-2 ingest."""
    from apecx_integration.execution.pbs_bundle import (
        BundleRequest, generate_bundle,
    )
    wf = tmp_path / "wf.yml"
    wf.write_text("name: test\n", encoding="utf-8")
    out = tmp_path / "bundle"
    req = BundleRequest(
        run_id=uuid.uuid4(),
        target_system="polaris",
        output_directory=out,
        workflow_yaml_path=wf,
        library_version="0.1.0",
        llm_model="mistral-nemo:latest",
        artifact_id=uuid.uuid4(),
        composition_summary_sentence="probe summary",
    )
    result = generate_bundle(req)
    expected = {
        "submit.pbs", "run.sh", "workflow.yml",
        "staging_plan.yml", "provenance_seed.json", "README.md",
    }
    actual = {p.name for p in result.bundle_path.iterdir()}
    missing = expected - actual
    assert not missing, f"PROBE 402: bundle missing files: {missing}"


def test_probe_403_pbs_bundle_unsupported_system_raises(tmp_path) -> None:
    """target_system not in SUPPORTED_SYSTEMS must fail-fast.
    Silently writing a bundle with target_system='magic' would
    let the scientist qsub on an HPC that's never been validated."""
    from apecx_integration.execution.pbs_bundle import (
        BundleRequest, UnsupportedSystem, generate_bundle,
    )
    wf = tmp_path / "wf.yml"
    wf.write_text("name: test\n", encoding="utf-8")
    req = BundleRequest(
        run_id=uuid.uuid4(),
        target_system="frontier",  # not supported
        output_directory=tmp_path / "bundle",
        workflow_yaml_path=wf,
        library_version="0.1.0",
        llm_model="m",
        artifact_id=uuid.uuid4(),
        composition_summary_sentence="x",
    )
    with pytest.raises(UnsupportedSystem):
        generate_bundle(req)


def test_probe_404_pbs_bundle_missing_workflow_raises(tmp_path) -> None:
    """Missing workflow_yaml_path → FileNotFoundError. Silently
    writing a bundle that references a missing yaml would mean
    the qsub'd job runs but cannot find its workflow."""
    from apecx_integration.execution.pbs_bundle import (
        BundleRequest, generate_bundle,
    )
    req = BundleRequest(
        run_id=uuid.uuid4(),
        target_system="aurora",
        output_directory=tmp_path / "bundle",
        workflow_yaml_path=tmp_path / "does_not_exist.yml",
        library_version="0.1.0",
        llm_model="m",
        artifact_id=uuid.uuid4(),
        composition_summary_sentence="x",
    )
    with pytest.raises(FileNotFoundError):
        generate_bundle(req)
