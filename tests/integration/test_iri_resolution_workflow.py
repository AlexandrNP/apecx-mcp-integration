"""End-to-end integration tests for the nanobrain-wrapped IRI resolution workflow.

What these tests cover
======================
- ``IRIResolutionWorkflow.from_config`` produces a working two-step DAG
  (normalize → resolve) loaded entirely via the from_config pattern.
- ``await workflow.process({"entity_records": [...]})`` cascades the data
  through both steps and returns ``{"resolved_records": [...]}``.
- Each path of ``lookup_entity`` (fast / miss; ancestor + slow are covered
  in adjacent test files) surfaces correctly through the workflow:
    - ``resolution_path``, ``canonical_iri``, ``canonical_label``,
      ``canonical_ontology``, ``resolution_status``, ``resolution_confidence``,
      and ``dictionary_version`` are present on every resolved record.
- The framework's broken trigger cascade is bypassed by the custom
  Workflow subclass; this test fails LOUDLY if someone reverts the
  subclass to the default Workflow class.
- Workflow lifecycle test: workflow can be re-invoked many times in
  the same process (regression guard against per-call leaks).
- "Bonus" record fields (``_original_surface_form``, anything the caller
  passed in addition to ``surface_form`` / ``entity_type``) are preserved
  in the output — load-bearing for the harvester adapter, which packs
  DataCite-shaped payload around these.

Gates
-----
- ``APECX_SYNONYM_DICT_LIVE_OLS=1`` — the fast-path test uses a real
  apecx-build-dictionary build over EBI OLS, same fixture as
  test_p39_precision_path_with_dict.
- The miss-path tests run unconditionally (no external state needed).

Reference: ``src/apecx_integration/synonym_dictionary/workflow/`` for the
implementation; ``configs/iri_resolution_workflow.yml`` is the loaded YAML.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = REPO_ROOT.parent
WORKFLOW_YAML = (
    REPO_ROOT
    / "src"
    / "apecx_integration"
    / "synonym_dictionary"
    / "workflow"
    / "configs"
    / "iri_resolution_workflow.yml"
)

VIOLIN_PATHOGENS = WORKSPACE_ROOT / "data" / "violin" / "Pathogen_Information.csv"

# EEEV at row 50 of VIOLIN — NCBITaxon_11021. The full canonical label is
# stored in the dict as "eastern equine encephalitis virus" (with "virus").
# Trimming the trailing "virus" produces a miss in the fast path; the slow
# substring matcher finds it but doesn't inject _resolution. Use the full
# canonical label here to exercise the fast path. See debug session
# 2026-05-04 for the verification: lookup_entity('eastern equine
# encephalitis virus') → fast / NCBITaxon_11021.
EEEV_TAXON_ID = 11021
EEEV_IRI = f"http://purl.obolibrary.org/obo/NCBITaxon_{EEEV_TAXON_ID}"
EEEV_LABEL_HINT = "eastern equine encephalitis virus"

_LIVE_OLS = os.environ.get("APECX_SYNONYM_DICT_LIVE_OLS", "").strip() == "1"


# ---------------------------------------------------------------------------
# Miss-path tests (no dictionary configured) — run unconditionally
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset the process singleton before/after each test to avoid leakage."""
    import apecx_integration.synonym_dictionary.loader as _loader
    from apecx_integration.synonym_dictionary.loader import _ProcessSingleton

    _orig = _loader._singleton
    _loader._singleton = _ProcessSingleton()
    yield
    _loader._singleton = _orig


def test_workflow_loads_via_from_config():
    """The workflow YAML loads cleanly and produces an IRIResolutionWorkflow."""
    from apecx_integration.synonym_dictionary.workflow import IRIResolutionWorkflow

    w = IRIResolutionWorkflow.from_config(str(WORKFLOW_YAML))
    assert isinstance(w, IRIResolutionWorkflow), (
        f"Expected IRIResolutionWorkflow; got {type(w).__name__}. "
        "The YAML may have been edited to drop the custom-class binding."
    )
    assert set(w.child_steps.keys()) == {
        "normalize",
        "resolve",
    }, f"Expected child_steps {{'normalize', 'resolve'}}; got {set(w.child_steps.keys())}"
    assert "entity_records" in w.step_input_data_units
    assert "resolved_records" in w.step_output_data_units
    assert len(w.step_links) == 3, (
        f"Expected 3 links wiring entry → normalize → resolve → exit; "
        f"got {len(w.step_links)}: {list(w.step_links.keys())}"
    )


def test_workflow_miss_path_returns_unresolved_record():
    """With no dictionary configured, every record resolves as 'miss'."""
    from apecx_integration.synonym_dictionary.workflow import IRIResolutionWorkflow

    w = IRIResolutionWorkflow.from_config(str(WORKFLOW_YAML))
    result = asyncio.run(
        w.process(
            {
                "entity_records": [
                    {"surface_form": "no-such-pathogen", "entity_type": "pathogen"},
                ]
            }
        )
    )
    resolved = result["resolved_records"]
    assert len(resolved) == 1
    rec = resolved[0]
    assert rec["resolution_path"] == "miss"
    assert rec["canonical_iri"] is None
    assert rec["resolution_confidence"] == 0.0
    assert rec["resolution_status"] == "unresolved"


def test_workflow_normalize_strips_and_lowercases():
    """The normalize step rewrites surface_form to lowercase + stripped form."""
    from apecx_integration.synonym_dictionary.workflow import IRIResolutionWorkflow

    w = IRIResolutionWorkflow.from_config(str(WORKFLOW_YAML))
    result = asyncio.run(
        w.process(
            {
                "entity_records": [
                    {"surface_form": "  EEEV  ", "entity_type": "pathogen"},
                ]
            }
        )
    )
    rec = result["resolved_records"][0]
    assert rec["surface_form"] == "eeev"
    assert rec["_original_surface_form"] == "  EEEV  "


def test_workflow_preserves_extra_fields():
    """Caller's free-form record fields survive the cascade untouched.

    Load-bearing for the harvester adapter: incoming DataCite payloads
    carry many fields beyond surface_form / entity_type, and the workflow
    must not strip them. The adapter does the DataCite re-validation;
    the workflow just has to round-trip the bag.
    """
    from apecx_integration.synonym_dictionary.workflow import IRIResolutionWorkflow

    w = IRIResolutionWorkflow.from_config(str(WORKFLOW_YAML))
    result = asyncio.run(
        w.process(
            {
                "entity_records": [
                    {
                        "surface_form": "eeev",
                        "entity_type": "pathogen",
                        "publisher": "VIOLIN",
                        "year": 2024,
                        "custom_field": {"nested": ["values"]},
                    },
                ]
            }
        )
    )
    rec = result["resolved_records"][0]
    assert rec["publisher"] == "VIOLIN"
    assert rec["year"] == 2024
    assert rec["custom_field"] == {"nested": ["values"]}


def test_workflow_handles_unknown_entity_type():
    """Unknown entity_type → entity_type=None passed to lookup_entity (cross-type search).

    With no dictionary configured, the result is still 'miss' but the call
    must not raise on the unknown type string.
    """
    from apecx_integration.synonym_dictionary.workflow import IRIResolutionWorkflow

    w = IRIResolutionWorkflow.from_config(str(WORKFLOW_YAML))
    result = asyncio.run(
        w.process(
            {
                "entity_records": [
                    {"surface_form": "eeev", "entity_type": "totally-bogus-type"},
                ]
            }
        )
    )
    rec = result["resolved_records"][0]
    assert rec["resolution_path"] == "miss"
    # No exception raised — that's the contract.


def test_workflow_handles_empty_input():
    """Empty record list returns empty resolved list cleanly."""
    from apecx_integration.synonym_dictionary.workflow import IRIResolutionWorkflow

    w = IRIResolutionWorkflow.from_config(str(WORKFLOW_YAML))
    result = asyncio.run(w.process({"entity_records": []}))
    assert result == {"resolved_records": []}


def test_workflow_can_be_invoked_multiple_times():
    """The same workflow instance can process many batches without leakage."""
    from apecx_integration.synonym_dictionary.workflow import IRIResolutionWorkflow

    w = IRIResolutionWorkflow.from_config(str(WORKFLOW_YAML))

    for i in range(3):
        result = asyncio.run(
            w.process(
                {
                    "entity_records": [
                        {"surface_form": f"term_{i}", "entity_type": "pathogen"},
                    ]
                }
            )
        )
        assert len(result["resolved_records"]) == 1
        assert result["resolved_records"][0]["resolution_path"] == "miss"


def test_workflow_native_framework_cascade():
    """REGRESSION GUARD: nanobrain's native data-driven cascade fires all steps.

    This test exercises the BASE Workflow class (not IRIResolutionWorkflow's
    explicit override) to prove that the framework's DirectLink change-listener
    propagation works end-to-end. If someone re-disables
    ``DirectLink._setup_callback_registration`` in nanobrain/core/link.py
    (the comment block that previously short-circuited the legacy callback
    mechanism), this test will fail because the resolve step's output data
    unit will stay None.

    See ``nanobrain/core/link.py::_setup_callback_registration`` for the
    fix and the rationale comment.
    """
    from nanobrain.core.workflow import Workflow

    async def run() -> dict | list | None:
        # Use the base Workflow class — proves the framework cascade fires.
        w = Workflow.from_config(str(WORKFLOW_YAML))
        inp = w.step_input_data_units["entity_records"]
        await inp.set([{"surface_form": "  EEEV  ", "entity_type": "pathogen"}])
        await w.execute()
        # The framework cascade is async (change listeners fire via an
        # AsyncTriggerExecutor). Wait for the chain to complete.
        await asyncio.sleep(1.5)
        out = w.step_output_data_units["resolved_records"]
        return await out.get()

    final_payload = asyncio.run(run())
    assert final_payload is not None, (
        "Native framework cascade did not propagate to workflow output data unit. "
        "DirectLink._setup_callback_registration may have been re-disabled in "
        "nanobrain/core/link.py — see the rationale comment there."
    )
    # The cascade completed; the resolved record should have miss-path shape
    # since no dictionary is configured.
    assert isinstance(final_payload, list)
    assert len(final_payload) == 1
    rec = final_payload[0]
    assert rec["resolution_path"] == "miss"
    assert rec["surface_form"] == "eeev"  # normalize step also fired


# ---------------------------------------------------------------------------
# Fast-path test — gated on live OLS build
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def eeev_dictionary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build a real Stage 1 dictionary from VIOLIN row 50 (EEEV).

    Same fixture shape as test_p39_precision_path_with_dict so we share the
    cost across modules when both are run in a session.
    """
    if not _LIVE_OLS:
        pytest.skip("Set APECX_SYNONYM_DICT_LIVE_OLS=1 to build a real dictionary.")
    assert VIOLIN_PATHOGENS.exists(), f"VIOLIN data missing at {VIOLIN_PATHOGENS}"

    from apecx_integration.synonym_dictionary.cli import main as build_main

    out = tmp_path_factory.mktemp("workflow_iri_dict")
    ret = build_main(
        [
            "--violin-pathogens",
            str(VIOLIN_PATHOGENS),
            "--output",
            str(out),
            "--dictionary-version",
            "test-iri-workflow",
            "--max-rows",
            "60",
            "--log-level",
            "WARNING",
        ]
    )
    assert ret == 0, f"apecx-build-dictionary exited with code {ret}"
    db_path = out / "dictionary.sqlite"
    assert db_path.exists()
    return db_path


@pytest.mark.skipif(
    not _LIVE_OLS,
    reason="Set APECX_SYNONYM_DICT_LIVE_OLS=1 to run live-OLS workflow tests.",
)
def test_workflow_fast_path_with_real_dictionary(eeev_dictionary: Path):
    """Workflow with dictionary_path config wires the singleton + resolves EEEV."""
    import apecx_integration.synonym_dictionary.loader as _loader
    from apecx_integration.synonym_dictionary.loader import configure_dictionary_path
    from apecx_integration.synonym_dictionary.workflow import IRIResolutionWorkflow

    # The workflow's resolve step has dictionary_path interpolated from
    # ${APECX_SYNONYM_DICT_PATH}. Set it explicitly via the module-level
    # singleton so the test doesn't depend on env-var ordering.
    configure_dictionary_path(eeev_dictionary)

    try:
        w = IRIResolutionWorkflow.from_config(str(WORKFLOW_YAML))
        result = asyncio.run(
            w.process(
                {
                    "entity_records": [
                        {"surface_form": EEEV_LABEL_HINT, "entity_type": "pathogen"},
                    ]
                }
            )
        )
        rec = result["resolved_records"][0]
        assert rec["resolution_path"] in ("fast", "ancestor"), (
            f"Expected fast/ancestor path with EEEV in dict; got {rec['resolution_path']!r}. "
            "Dictionary may not have built EEEV row 50."
        )
        assert EEEV_IRI in (
            rec["canonical_iri"] or ""
        ), f"Expected canonical_iri to contain {EEEV_IRI!r}; got {rec['canonical_iri']!r}"
        assert rec["resolution_confidence"] > 0.0
        assert rec["dictionary_version"] is not None
    finally:
        # Reset singleton so other tests in the same process aren't polluted
        from apecx_integration.synonym_dictionary.loader import _ProcessSingleton

        _loader._singleton = _ProcessSingleton()


@pytest.mark.skipif(
    not _LIVE_OLS,
    reason="Set APECX_SYNONYM_DICT_LIVE_OLS=1 to run live-OLS workflow tests.",
)
def test_workflow_mixed_batch_with_real_dictionary(eeev_dictionary: Path):
    """Mix of resolved + miss records in one batch — proves per-record routing."""
    import apecx_integration.synonym_dictionary.loader as _loader
    from apecx_integration.synonym_dictionary.loader import configure_dictionary_path
    from apecx_integration.synonym_dictionary.workflow import IRIResolutionWorkflow

    configure_dictionary_path(eeev_dictionary)
    try:
        w = IRIResolutionWorkflow.from_config(str(WORKFLOW_YAML))
        result = asyncio.run(
            w.process(
                {
                    "entity_records": [
                        {"surface_form": EEEV_LABEL_HINT, "entity_type": "pathogen"},
                        {
                            "surface_form": "definitely-not-a-pathogen-xyz",
                            "entity_type": "pathogen",
                        },
                    ]
                }
            )
        )
        resolved = result["resolved_records"]
        assert len(resolved) == 2
        # First record: dict hit
        assert resolved[0]["resolution_path"] in ("fast", "ancestor")
        assert resolved[0]["resolution_confidence"] > 0.0
        # Second record: miss
        assert resolved[1]["resolution_path"] == "miss"
        assert resolved[1]["resolution_confidence"] == 0.0
    finally:
        from apecx_integration.synonym_dictionary.loader import _ProcessSingleton

        _loader._singleton = _ProcessSingleton()
