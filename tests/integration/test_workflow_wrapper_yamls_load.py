"""T02 Phase 2: each wrapper YAML loads via from_config without error.

Cheap correctness gate: catches class-path typos, schema-drift
between the YAML and the step class, and missing required fields —
all of which would otherwise surface only at workflow-composition
time.

Does NOT exercise the full workflow end-to-end (that's T01). Does
NOT require a live Control Plane (the steps instantiate cleanly
even with a placeholder base_url; real HTTP calls happen at
process() time, not init time).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from apecx_integration.composition.steps.synonym_cache import (
    SynonymCacheLookupStep,
    VerifiedSynonymWritebackStep,
)
from nanobrain.library.steps.approval_step import ApprovalStep

pytestmark = pytest.mark.integration

STEPS_DIR = (
    Path(__file__).resolve().parents[1].parent
    / "src"
    / "apecx_integration"
    / "composition"
    / "workflows"
    / "violin_bvbrc"
    / "steps"
)


@pytest.fixture(autouse=True)
def _stub_env(monkeypatch):
    """The step YAMLs reference ``${CONTROL_PLANE_URL}`` for env-var
    interpolation. Set a placeholder so the YAML loader doesn't choke
    on an undefined variable. Real value comes from the runtime
    environment in production.
    """
    monkeypatch.setenv("CONTROL_PLANE_URL", "http://localhost:8000")


def test_synonym_cache_lookup_yaml_loads(monkeypatch) -> None:
    path = STEPS_DIR / "synonym_cache_lookup.yml"
    assert path.is_file(), path
    step = SynonymCacheLookupStep.from_config(str(path))
    assert step.name == "synonym_cache_lookup"
    assert step._source_vocabulary == "user_query"
    assert step._target_vocabulary == "violin.pathogen_id"
    assert step._scope is None


def test_synonym_approval_gate_yaml_loads() -> None:
    path = STEPS_DIR / "synonym_approval_gate.yml"
    assert path.is_file(), path
    step = ApprovalStep.from_config(str(path))
    assert step.name == "synonym_approval_gate"
    # HARD gate per first-release directive; no timeout; on_timeout
    # still "reject" since it's the default even when not triggered.
    assert step._gate_kind == "hard"
    assert step._timeout_seconds is None


def test_verified_synonym_writeback_yaml_loads() -> None:
    path = STEPS_DIR / "verified_synonym_writeback.yml"
    assert path.is_file(), path
    step = VerifiedSynonymWritebackStep.from_config(str(path))
    assert step.name == "verified_synonym_writeback"
    assert step._source_vocabulary == "user_query"
    assert step._target_vocabulary == "violin.pathogen_id"
    assert step._verified_by == "api_user"


def test_all_three_steps_resolve_to_the_same_control_plane_base_url(
    monkeypatch,
) -> None:
    """The three Control Plane-integrated steps must all load with the
    same resolved ``base_url``. Memo 08 made ``ConfigBase`` interpolate
    ``${CONTROL_PLANE_URL:-http://localhost:8000}`` at load time, so
    this now asserts on the *resolved* value rather than the old
    literal-placeholder-passthrough behavior.

    When the env var is unset, all three resolve to the local-dev
    default. That's the coherence check: if one YAML silently drifts
    to a different default or a different env-var name, this catches
    it at step-construction time.
    """
    monkeypatch.delenv("CONTROL_PLANE_URL", raising=False)

    cache = SynonymCacheLookupStep.from_config(
        str(STEPS_DIR / "synonym_cache_lookup.yml")
    )
    gate = ApprovalStep.from_config(str(STEPS_DIR / "synonym_approval_gate.yml"))
    writeback = VerifiedSynonymWritebackStep.from_config(
        str(STEPS_DIR / "verified_synonym_writeback.yml")
    )

    expected = "http://localhost:8000"
    assert cache._control_plane_config["base_url"] == expected
    assert gate._base_url == expected
    assert writeback._control_plane_config["base_url"] == expected


def test_env_var_override_propagates_to_all_three_steps(monkeypatch) -> None:
    """Setting ``CONTROL_PLANE_URL`` overrides the default in all three
    YAMLs. This is the BYO-infra contract promised in memo 08: one env
    var, all affected steps retarget together — no per-YAML edits.
    """
    override = "https://apecx-cp.example.invalid:9999"
    monkeypatch.setenv("CONTROL_PLANE_URL", override)

    cache = SynonymCacheLookupStep.from_config(
        str(STEPS_DIR / "synonym_cache_lookup.yml")
    )
    gate = ApprovalStep.from_config(str(STEPS_DIR / "synonym_approval_gate.yml"))
    writeback = VerifiedSynonymWritebackStep.from_config(
        str(STEPS_DIR / "verified_synonym_writeback.yml")
    )

    assert cache._control_plane_config["base_url"] == override
    assert gate._base_url == override
    assert writeback._control_plane_config["base_url"] == override
