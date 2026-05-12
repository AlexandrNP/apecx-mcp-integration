"""EXPT-RT — spec-mode adoption gate.

Mirrors ``test_t01_ac1_against_ollama.py`` but with
``APECX_COMPOSER_MODE=spec``. Drives the full end-to-end
roundtrip: real Ollama → compose() → persist → LocalExecutor.execute()
→ assert RUN_COMPLETED.

**This is the empirical gate for flipping spec mode to the
default.** If both the existing AC1 test (monolithic) AND this
test (spec) reach RUN_COMPLETED on the same model + machine, spec
mode is at least as good as monolithic for the structural shape
of a real workflow run — not just for compose() returning a
ComposedWorkflow.

Brutal-truth distinction from the diagnostic E2E test in
``test_composer_validator_e2e_against_ollama.py``: that file asserts
the MACHINERY's diagnostic surface is correct in any outcome. This
file asserts the WORKFLOW ACTUALLY EXECUTES. The diagnostic test
can pass with a generated workflow that doesn't load; this test
can't.

Run under the venv:

    APECX_LLM_BASE_URL=http://localhost:11434/v1 \\
    APECX_LLM_MODEL=mistral-nemo:latest \\
    APECX_LLM_TEMPERATURE=0.0 APECX_LLM_MAX_TOKENS=2048 \\
    APECX_COMPOSER_MODE=spec \\
    PYTHONPATH=src .venv/bin/python -m pytest \\
      tests/integration/test_spec_mode_ac1_against_ollama.py -v -s

Auto-skips when Ollama is unreachable or nanobrain isn't on the
PYTHONPATH (workspace friction-log rule).
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

try:
    import nanobrain.core.workflow  # noqa: F401

    _DEPS_OK = True
except ImportError:
    _DEPS_OK = False

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSER_CONFIG = REPO_ROOT / "src" / "apecx_integration" / "composition" / "composer_config.yml"
VIOLIN_WORKFLOW_DIR = (
    REPO_ROOT / "src" / "apecx_integration" / "composition" / "workflows" / "violin_bvbrc"
)


def _llm_reachable() -> bool:
    base = os.environ.get("APECX_LLM_BASE_URL") or "http://localhost:11434/v1"
    probe = base[:-3] + "/api/tags" if base.endswith("/v1") else base.rstrip("/") + "/api/tags"
    try:
        return httpx.get(probe, timeout=2.0).status_code == 200
    except Exception:
        return False


SKIP_DEPS = "nanobrain not importable — run under the venv"
SKIP_LLM = (
    "LLM not reachable — set APECX_LLM_BASE_URL and make sure ollama serves the requested model"
)


@pytest.mark.skipif(not _DEPS_OK, reason=SKIP_DEPS)
@pytest.mark.skipif(not _llm_reachable(), reason=SKIP_LLM)
def test_spec_mode_ac1_real_workflow_runs(cp_engine, monkeypatch):
    """Spec-mode equivalent of AC1: compose + execute + reach
    RUN_COMPLETED.

    The prompt mirrors the AC1 wording so the LLM's behavior is
    comparable. Spec mode swap is forced via monkeypatch on the env
    var so the test is reproducible regardless of the operator's
    shell environment.
    """
    monkeypatch.setenv("APECX_COMPOSER_MODE", "spec")

    from apecx_integration.composition.artifact_store import ArtifactStore
    from apecx_integration.composition.composer import Composer
    from apecx_integration.control_plane.db import make_session_factory
    from apecx_integration.control_plane.executors.local import (
        LocalExecutor,
        run_sync,
    )
    from apecx_integration.control_plane.models.entities import (
        Run as RunORM,
    )
    from apecx_integration.control_plane.provenance.recorder import (
        ProvenanceRecorder,
    )
    from apecx_integration.control_plane.schemas.enums import RunStatus

    factory = make_session_factory(cp_engine)
    recorder = ProvenanceRecorder(factory)
    store = ArtifactStore(session_factory=factory, recorder=recorder)
    composer = Composer.from_config(COMPOSER_CONFIG)
    composer._artifact_store = store
    # Sanity: confirm the env-var override actually landed in the config.
    assert composer._config.composer_mode == "spec", (
        "spec-mode env-var override did not flow through Composer.from_config"
    )
    executor = LocalExecutor(
        session_factory=factory,
        artifact_store=store,
        recorder=recorder,
        workflow_base_dir=VIOLIN_WORKFLOW_DIR,
        # EMPTY-FAIL (2026-05-12): empty-input opt-in is needed
        # because this fixture exercises the spec-composer +
        # executor cascade plumbing, NOT a real workflow's data
        # processing. A real adoption test in production would
        # pass default_payload={"user_query_input": "..."}. See
        # the EMPTY-FAIL commit message + the executor's
        # _allow_empty_input docstring.
        allow_empty_input=True,
    )

    run_id = uuid4()
    with factory() as session:
        session.add(
            RunORM(
                id=run_id,
                user_id="spec_mode_test",
                status=RunStatus.PENDING,
                created_at=datetime.now(UTC),
            )
        )
        session.commit()

    prompt = (
        "Extract pathogen entity names from a biomedical query and "
        "map them to BV-BRC genome ids using the local snapshot."
    )
    composed = asyncio.run(composer.compose(prompt, context={"run_id": run_id}))

    with factory() as session:
        run = session.get(RunORM, run_id)
        run.workflow_config_id = composed.artifact_id
        run.status = RunStatus.RUNNING
        session.commit()

    result = run_sync(executor, run_id)

    # The adoption gate: RUN_COMPLETED on the real workflow under
    # spec mode. If this fails, the spec composer's YAML didn't
    # load or didn't execute correctly — flipping the default would
    # break adoption.
    print(
        f"\n[EXPT-RT] spec-mode AC1 outcome: status={result.status} "
        f"compose_retries={composed.composition_summary.compose_retries} "
        f"summary={composed.composition_summary.summary_sentence!r} "
        f"reason={result.reason!r}"
    )
    assert result.status is RunStatus.COMPLETED, (
        f"spec-mode AC1 violation: expected RUN_COMPLETED; got "
        f"{result.status} reason={result.reason!r}; the generated "
        f"workflow YAML was:\n{composed.yaml_bytes.decode('utf-8')}"
    )
    assert result.output_artifact_id is not None
    recorder.validate(run_id)
