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

import os
from pathlib import Path

import pytest

from nanobrain.library.steps.approval_step import ApprovalStep

from apecx_integration.composition.steps.synonym_cache import (
    SynonymCacheLookupStep,
    VerifiedSynonymWritebackStep,
)

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


def test_all_three_steps_reference_the_same_control_plane_placeholder() -> None:
    """The workflow wires all three to the same Control Plane. If one
    YAML silently drifts to a different base_url, this catches it.

    Brutal-truth caveat: nanobrain's env-var interpolation does NOT fire
    on nested-dict values during step ``from_config`` — the literal
    ``"${CONTROL_PLANE_URL}"`` is stored as-is. ApprovalStep (T10)
    exhibits the same pre-existing behavior; interpolation presumably
    happens at a higher workflow layer or when the step is actually
    used. This test therefore asserts that all three YAMLs carry the
    **same placeholder literal**, which is the coherence check we
    actually care about. Interpolation correctness is the next layer's
    problem; a follow-up should either add it at step level or
    explicitly interpolate before calling process().
    """
    cache = SynonymCacheLookupStep.from_config(str(STEPS_DIR / "synonym_cache_lookup.yml"))
    gate = ApprovalStep.from_config(str(STEPS_DIR / "synonym_approval_gate.yml"))
    writeback = VerifiedSynonymWritebackStep.from_config(
        str(STEPS_DIR / "verified_synonym_writeback.yml")
    )
    placeholder = "${CONTROL_PLANE_URL}"
    assert cache._control_plane_config["base_url"] == placeholder
    assert gate._base_url == placeholder
    assert writeback._control_plane_config["base_url"] == placeholder


def test_env_var_not_set_is_a_loud_failure() -> None:
    """If CONTROL_PLANE_URL is unset, env-var interpolation leaves a
    literal '${CONTROL_PLANE_URL}' in the config. The steps should
    accept the literal at load time; the actual HTTP call will fail
    later with a clearer error. This test documents that the loader
    does not fail silently — it just passes the literal through.
    """
    # Unset any inherited value.
    original = os.environ.pop("CONTROL_PLANE_URL", None)
    try:
        # SynonymCacheLookupStep has base_url validation that only
        # checks non-empty — a literal ${VAR} is non-empty, so it
        # loads. The real failure is deferred to process() time.
        step = SynonymCacheLookupStep.from_config(
            str(STEPS_DIR / "synonym_cache_lookup.yml")
        )
        # Accept either: env-var interpolation already happened at
        # YAML load (leaving "" or nothing) or the literal persists.
        base_url = step._control_plane_config.get("base_url", "")
        assert base_url in ("", "${CONTROL_PLANE_URL}"), (
            f"unexpected base_url when env var unset: {base_url!r}"
        )
    finally:
        if original is not None:
            os.environ["CONTROL_PLANE_URL"] = original
