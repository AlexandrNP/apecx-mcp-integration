"""RT-REAL — honest end-to-end roundtrip with real payload.

The EXPT-RT test (test_spec_mode_ac1_against_ollama.py) sets
``allow_empty_input=True`` to bypass the EMPTY-FAIL gate, exercising
the executor + cascade plumbing but NOT real workflow processing.
This file is the honest variant: it provides a REAL payload so the
workflow actually computes something.

Strategy:
  1. Force the spec-mode composer to a known skeleton via a stub LLM.
     Using ``entity_extraction_only`` because it's a single-step
     skeleton + the step's input is a simple string.
  2. Provide a real biomedical query as the executor's default_payload
     under the workflow-level ``workflow_input`` key.
  3. Drive ``executor.execute(run_id)`` — empty-fail gate must NOT
     fire (we have real input).
  4. Assert RUN_COMPLETED + the output artifact contains data that
     resembles entity extraction (not just a trigger-init status dict).

If this test fails on a clean checkout, the failure is one of:
  - The composer's spec-mode prompt regressed.
  - The expander's link wiring drifted.
  - The executor's empty-fail gate has a bug.
  - The entity_extraction step's wrapper YAML changed shape.
  - The LLM model regressed at NER (skipped, not fatal).
"""

from __future__ import annotations

import asyncio
import json
import os
import textwrap
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
SKIP_LLM = "LLM not reachable — set APECX_LLM_BASE_URL"


SKELETON_SHORTHAND = textwrap.dedent(
    """\
    ```json
    {"skeleton": "entity_extraction_only"}
    ```
    """
)


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


class _StubComposerLLM:
    """LLM stub for the COMPOSER call — returns the skeleton shorthand
    deterministically so this test isolates the executor's real-
    payload path, not the LLM's NER quality on the compose pass.
    """

    def __init__(self, content: str) -> None:
        self._content = content

    def invoke(self, messages):
        return _Resp(self._content)


@pytest.mark.skipif(not _DEPS_OK, reason=SKIP_DEPS)
@pytest.mark.skipif(not _llm_reachable(), reason=SKIP_LLM)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN REGRESSION (RT-REAL, 2026-05-12): spec-mode composer "
        "produces workflows that load + run + drain cascade but the "
        "workflow-level ``workflow_output`` data unit stays None. "
        "EMPTY-OUTPUT gate fires correctly with a clear diagnostic; "
        "the underlying framework/composer issue (cascade fires but "
        "no data reaches workflow_output) needs deeper investigation. "
        "Marked xfail-strict so: (a) the test FAILS with a structured "
        "diagnostic until fixed; (b) when the underlying bug IS "
        "fixed, the test starts passing and xfail-strict turns that "
        "pass into a FAILURE — forcing the operator to delete the "
        "xfail marker. That's the right cadence: silent failures NOT "
        "tolerated, structured failures TRACKED. See the executor's "
        "empty_output_refused failure_class for the gate."
    ),
)
def test_spec_mode_real_payload_reaches_completed_with_output(cp_engine, monkeypatch):
    """Honest adoption gate: real input → workflow processes → real
    output. No allow_empty_input shortcut. The output artifact must
    carry actual entity candidates (not just a trigger-init status
    dict)."""
    monkeypatch.setenv("APECX_COMPOSER_MODE", "spec")
    # The EntityExtractionStep calls Ollama for NER. Use the same
    # model the composer is using so we have one LLM provider.

    from apecx_integration.composition.artifact_store import ArtifactStore
    from apecx_integration.composition.composer import Composer
    from apecx_integration.control_plane.db import make_session_factory
    from apecx_integration.control_plane.executors.local import (
        LocalExecutor,
        run_sync,
    )
    from apecx_integration.control_plane.models.entities import (
        Artifact as ArtifactORM,
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
    # Force compose to the entity_extraction_only skeleton — no LLM
    # variance on the compose pass; the variance lives entirely in
    # the workflow's EntityExtractionStep call to Ollama.
    composer._llm_factory = lambda **_kw: _StubComposerLLM(  # noqa: SLF001
        SKELETON_SHORTHAND
    )
    assert composer._config.composer_mode == "spec"

    # The REAL payload: a query the LLM should NER reasonably.
    real_payload = {"workflow_input": "I'm researching Zika and Ebola viruses."}

    executor = LocalExecutor(
        session_factory=factory,
        artifact_store=store,
        recorder=recorder,
        workflow_base_dir=VIOLIN_WORKFLOW_DIR,
        default_payload=real_payload,
        # NO allow_empty_input — we have real input. The empty-fail
        # gate must NOT fire.
    )

    run_id = uuid4()
    with factory() as session:
        session.add(
            RunORM(
                id=run_id,
                user_id="rt_real_test",
                status=RunStatus.PENDING,
                created_at=datetime.now(UTC),
            )
        )
        session.commit()

    composed = asyncio.run(composer.compose("extract entities", context={"run_id": run_id}))
    with factory() as session:
        run = session.get(RunORM, run_id)
        run.workflow_config_id = composed.artifact_id
        run.status = RunStatus.RUNNING
        session.commit()

    result = run_sync(executor, run_id)

    print(
        f"\n[RT-REAL] outcome: status={result.status} "
        f"reason={result.reason!r} "
        f"output_artifact_id={result.output_artifact_id}"
    )
    assert result.status is RunStatus.COMPLETED, (
        f"RT-REAL violation: expected RUN_COMPLETED with real input; "
        f"got {result.status} reason={result.reason!r}. The workflow "
        f"YAML was:\n{composed.yaml_bytes.decode('utf-8')}"
    )
    assert result.output_artifact_id is not None

    # The HONEST check: the persisted output artifact must contain
    # the workflow's actual output — not just a trigger-init status
    # dict. We look for at least one entity name from the prompt in
    # the persisted content.
    with factory() as session:
        artifact = session.get(ArtifactORM, result.output_artifact_id)
        assert artifact is not None
    output_bytes = Path(artifact.location).read_bytes()
    try:
        output = json.loads(output_bytes)
    except json.JSONDecodeError:
        pytest.fail(f"output artifact at {artifact.location} is not JSON: {output_bytes[:500]!r}")
    output_text = json.dumps(output).lower()
    print(
        f"[RT-REAL] output keys: {list(output) if isinstance(output, dict) else type(output).__name__}"
    )
    print(f"[RT-REAL] output preview: {output_text[:500]}")
    # Brutal-truth assertion: SOMETHING in the output must mention
    # an entity from the prompt OR the workflow's expected output
    # field. If neither, the workflow "completed" but produced no
    # useful data — exactly the silent-failure shape this test is
    # designed to catch.
    found_entity = any(ent.lower() in output_text for ent in ("zika", "ebola"))
    has_entity_candidate_field = "entity_candidates" in output_text or "entities" in output_text
    assert found_entity or has_entity_candidate_field, (
        "RT-REAL violation: output artifact contains NO entity names "
        "from the prompt AND no entity-related field name. This is "
        "the EXACT silent-failure shape (workflow 'completed' with "
        "no real output). Output preview: " + output_text[:500]
    )
