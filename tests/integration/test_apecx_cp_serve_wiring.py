"""Regression guard for ``_build_components_from_env``.

Pre-2026-04-25 ``apecx-cp serve`` ran the module-level
``app = create_app()`` (no kwargs) and ignored every ``APECX_*_PATH``
env var the 503 messages promised would configure the components.
Result: every operator following the tutorial hit 503 on the first
``/workflows/start`` call.

The fix in commit 09ed... wires composer + approval policy + local
executor from env vars at boot, with sane defaults pointing at
in-repo configs. These tests pin both the default-paths-found
happy path and the env-var-empty-disable path.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def cp_engine_in_memory(tmp_path: Path):
    """Fresh migrated SQLite for component-wiring tests."""
    from alembic import command
    from alembic.config import Config
    from apecx_integration.control_plane.db import make_engine

    REPO_ROOT = Path(__file__).resolve().parents[2]
    db_file = tmp_path / "wiring.db"
    url = f"sqlite:///{db_file}"
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    command.upgrade(cfg, "head")
    return make_engine(url)


def test_build_components_from_env_uses_defaults_when_env_unset(
    cp_engine_in_memory, monkeypatch
):
    """With no APECX_*_PATH env vars set, the helper should resolve
    every component from the in-repo default paths and return
    non-None for all three.
    """
    from apecx_integration.composition.approval_policy import ApprovalPolicy
    from apecx_integration.composition.composer import Composer
    from apecx_integration.control_plane.app import _build_components_from_env
    from apecx_integration.control_plane.executors.local import LocalExecutor

    for var in (
        "APECX_COMPOSER_CONFIG_PATH",
        "APECX_APPROVAL_POLICY_PATH",
        "APECX_WORKFLOW_BASE_DIR",
    ):
        monkeypatch.delenv(var, raising=False)

    composer, policy, executor = _build_components_from_env(cp_engine_in_memory)

    assert isinstance(composer, Composer), (
        "composer must be wired from default config — pre-fix this was None."
    )
    assert isinstance(policy, ApprovalPolicy)
    assert isinstance(executor, LocalExecutor)
    # ArtifactStore was injected post-construction — check that the
    # composer has it so the persist path works.
    assert composer._artifact_store is not None  # noqa: SLF001


def test_build_components_from_env_disables_via_empty_string(
    cp_engine_in_memory, monkeypatch
):
    """Setting an APECX_*_PATH to empty string explicitly disables
    that component (route returns 503). This is the documented
    escape hatch for operators who want a Control Plane without
    the LLM stack.
    """
    from apecx_integration.control_plane.app import _build_components_from_env

    monkeypatch.setenv("APECX_COMPOSER_CONFIG_PATH", "")
    monkeypatch.setenv("APECX_APPROVAL_POLICY_PATH", "")
    monkeypatch.setenv("APECX_WORKFLOW_BASE_DIR", "")

    composer, policy, executor = _build_components_from_env(cp_engine_in_memory)

    assert composer is None
    assert policy is None
    assert executor is None


def test_build_components_returns_none_on_missing_path(
    cp_engine_in_memory, monkeypatch, tmp_path
):
    """If the operator points an env var at a non-existent path, the
    helper returns None for that component without crashing — the
    matching route surfaces 503 as before, but the OTHER components
    still resolve from their defaults.

    A WARNING is logged at the offending site so operators have a
    paper-trail (verified manually in the implementation; not asserted
    here because asserting on log text without behavior is brittle
    per audit §4.1, and the behavior-side assertion below is the
    load-bearing check).
    """
    from apecx_integration.control_plane.app import _build_components_from_env

    monkeypatch.setenv("APECX_COMPOSER_CONFIG_PATH", str(tmp_path / "no_such.yml"))
    monkeypatch.delenv("APECX_APPROVAL_POLICY_PATH", raising=False)
    monkeypatch.delenv("APECX_WORKFLOW_BASE_DIR", raising=False)

    composer, policy, executor = _build_components_from_env(cp_engine_in_memory)

    assert composer is None, "missing config file must NOT crash; just None."
    # Other components still resolve from defaults — partial wiring is
    # acceptable.
    assert policy is not None
    assert executor is not None


def test_serve_wires_app_so_workflows_start_does_not_503(
    cp_engine_in_memory, monkeypatch
):
    """End-to-end behavioral guard: after ``_build_components_from_env``
    is called and the result is passed into ``create_app``, the
    ``/workflows/start`` route must NOT return 503 for the
    "Composer is not configured" reason. The route can still return
    other statuses (it'll fail composer.compose() for a stub LLM,
    etc.) — what matters here is that the 503 the user originally
    hit no longer fires.
    """
    from fastapi.testclient import TestClient

    from apecx_integration.control_plane.app import (
        _build_components_from_env,
        create_app,
    )

    for var in (
        "APECX_COMPOSER_CONFIG_PATH",
        "APECX_APPROVAL_POLICY_PATH",
        "APECX_WORKFLOW_BASE_DIR",
    ):
        monkeypatch.delenv(var, raising=False)

    composer, policy, executor = _build_components_from_env(cp_engine_in_memory)
    wired = create_app(
        engine=cp_engine_in_memory,
        composer=composer,
        approval_policy=policy,
        local_executor=executor,
    )

    # Stub out the LLM factory so /workflows/start doesn't try to
    # reach Ollama in CI. We're checking the WIRING, not the
    # composer's actual LLM behavior.
    class _StubResp:
        content = "```yaml\nname: x\ndescription: x\nversion: '0.1.0'\nsteps: {}\nlinks: {}\n```"

    class _StubLLM:
        def invoke(self, _msgs):
            return _StubResp()

    composer._llm_factory = lambda **_kw: _StubLLM()  # noqa: SLF001

    client = TestClient(wired)
    resp = client.post(
        "/workflows/start",
        json={"description": "wiring smoke test", "user_id": "alex"},
    )
    # 200 (success) or 422 (composer-response-validation failure on
    # the stubbed YAML) is acceptable here. What we MUST NOT see is
    # 503 with the "Composer is not configured" detail — that's the
    # exact bug this cluster fixes.
    assert resp.status_code != 503, (
        f"Wired app still returned 503: {resp.text}. _build_components_"
        "from_env should have set app.state.composer."
    )
    if resp.status_code != 200:
        # Surface the body so a future regression has a useful trace.
        assert "Composer is not configured" not in resp.text
