"""CGU-P0-T2 — pin the resolution order for ``resolve_role``.

Resolution order (narrowest-wins):

1. Explicit Python kwarg.
2. ``APECX_LLM_MODEL_<ROLE>`` env var.
3. ``composer_config.yml`` ``model_roles.<role>`` entry.
4. ``APECX_LLM_MODEL`` env var (single-model fallback).
5. Hardcoded role default.

Each test pins one layer + verifies that wider layers do not
override it. Tests are pure (no Ollama) — the resolver returns a
string, no LLM is contacted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.benchmarks.model_roles import _clear_role_cache, resolve_role


@pytest.fixture(autouse=True)
def _reset_cache_each_test(monkeypatch):
    """Clear the role cache before AND after each test.

    Each test mutates env vars or the config path; without the
    reset, cache-induced cross-test bleed produces false greens.
    """
    _clear_role_cache()
    yield
    _clear_role_cache()


# Path to a temp YAML the tests write under tmp_path. The resolver
# accepts a ``config_path`` kwarg so we never depend on the live
# composer_config.yml during unit tests.
def _write_config(tmp_path: Path, model_roles: dict) -> Path:
    import yaml

    path = tmp_path / "composer_config.yml"
    path.write_text(yaml.safe_dump({"model_roles": model_roles}))
    return path


def test_layer_1_explicit_kwarg_wins(monkeypatch, tmp_path):
    """A caller passing kwarg_model overrides every other source."""
    monkeypatch.setenv("APECX_LLM_MODEL_DRAFTER", "from-env-role")
    monkeypatch.setenv("APECX_LLM_MODEL", "from-env-generic")
    cfg = _write_config(tmp_path, {"drafter": {"model": "from-config"}})

    model, _ = resolve_role("drafter", kwarg_model="explicit-kwarg", config_path=cfg)
    assert model == "explicit-kwarg"


def test_layer_2_role_env_beats_config_and_generic(monkeypatch, tmp_path):
    monkeypatch.setenv("APECX_LLM_MODEL_DRAFTER", "from-env-role")
    monkeypatch.setenv("APECX_LLM_MODEL", "from-env-generic")
    cfg = _write_config(tmp_path, {"drafter": {"model": "from-config"}})

    model, _ = resolve_role("drafter", config_path=cfg)
    assert model == "from-env-role"


def test_layer_3_config_beats_generic_env_and_hardcoded(monkeypatch, tmp_path):
    """When no role-specific env is set, the YAML entry wins over
    the generic env var and the hardcoded default."""
    monkeypatch.delenv("APECX_LLM_MODEL_DRAFTER", raising=False)
    monkeypatch.setenv("APECX_LLM_MODEL", "from-env-generic")
    cfg = _write_config(tmp_path, {"drafter": {"model": "from-config"}})

    model, _ = resolve_role("drafter", config_path=cfg)
    assert model == "from-config"


def test_layer_4_generic_env_wins_when_config_missing_role(monkeypatch, tmp_path):
    monkeypatch.delenv("APECX_LLM_MODEL_PLANNER", raising=False)
    monkeypatch.setenv("APECX_LLM_MODEL", "from-env-generic")
    cfg = _write_config(tmp_path, {"drafter": {"model": "from-config"}})

    model, _ = resolve_role("planner", config_path=cfg)
    assert model == "from-env-generic"


def test_layer_5_hardcoded_default_is_last_resort(monkeypatch, tmp_path):
    """All four upper layers absent → fall back to hardcoded."""
    monkeypatch.delenv("APECX_LLM_MODEL_PLANNER", raising=False)
    monkeypatch.delenv("APECX_LLM_MODEL", raising=False)
    cfg = _write_config(tmp_path, {"drafter": {"model": "from-config"}})

    model, _ = resolve_role("planner", config_path=cfg)
    # Hardcoded planner default per docs/composer_codegen_uplift_plan.md.
    assert model == "nemotron-3-nano:4b"


def test_base_url_inherited_from_config_when_present(monkeypatch, tmp_path):
    """A ``base_url`` in the YAML model_roles entry is returned when
    no kwarg / env override exists."""
    monkeypatch.delenv("APECX_LLM_BASE_URL", raising=False)
    cfg = _write_config(
        tmp_path,
        {"drafter": {"model": "from-config", "base_url": "http://config-host/v1"}},
    )
    _, base = resolve_role("drafter", config_path=cfg)
    assert base == "http://config-host/v1"


def test_base_url_falls_back_to_default_when_nothing_set(monkeypatch, tmp_path):
    monkeypatch.delenv("APECX_LLM_BASE_URL", raising=False)
    cfg = _write_config(tmp_path, {"drafter": {"model": "from-config"}})
    _, base = resolve_role("drafter", config_path=cfg)
    assert base == "http://localhost:11434/v1"


def test_unknown_role_raises(monkeypatch, tmp_path):
    """A typo in the role name fails loud rather than silently
    defaulting to drafter. Mirrors the workspace's extra='forbid'
    rule for pydantic configs."""
    cfg = _write_config(tmp_path, {"drafter": {"model": "from-config"}})
    with pytest.raises(KeyError, match="Unknown codegen role"):
        resolve_role("draftor", config_path=cfg)


def test_malformed_config_does_not_break_resolution(monkeypatch, tmp_path):
    """A malformed model_roles entry should NOT prevent the resolver
    from falling back to env vars. Otherwise a bad composer_config.yml
    would break the benchmark harness's ability to run a smoke sweep."""
    monkeypatch.delenv("APECX_LLM_MODEL", raising=False)
    bad = tmp_path / "composer_config.yml"
    bad.write_text("model_roles: {not_a_dict: true}\n")

    # Resolver returns a usable answer — the hardcoded default —
    # rather than raising.
    model, _ = resolve_role("drafter", config_path=bad)
    assert model == "mistral-nemo:latest"


def test_live_composer_config_has_three_roles_wired():
    """Regression guard: the shipped composer_config.yml must declare
    drafter / planner / reviewer. If a future edit drops one, the
    benchmark codegens silently revert to hardcoded defaults — that
    is the silent-failure mode this test exists to catch."""
    from tests.benchmarks.model_roles import _DEFAULT_CONFIG_PATH, _load_config_roles

    roles = _load_config_roles(_DEFAULT_CONFIG_PATH)
    assert "drafter" in roles, "composer_config.yml must declare model_roles.drafter"
    assert "planner" in roles, "composer_config.yml must declare model_roles.planner"
    assert "reviewer" in roles, "composer_config.yml must declare model_roles.reviewer"
