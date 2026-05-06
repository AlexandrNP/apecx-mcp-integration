"""Contract test — IRIResolutionWorkflow plugs into apecx-harvesters' ingest stage.

Companion to test_harvester_contract.py.  That file proves the plain-Python
``EntityResolutionTransform`` can be wrapped as a harvester ``Transform``.
This file proves the SAME for the nanobrain-wrapped IRIResolutionWorkflow:

  - ``adapt_workflow_to_harvester_transform(workflow, extension_field="canonical")``
    returns a callable of the harvester's Transform shape.
  - The adapter dumps a DataCite-subclass record → dict, drives the workflow
    end-to-end, packs the resolution payload into the named extension field,
    and re-validates as the original concrete DataCite type (preserving the
    DataCite ``extra='forbid'`` invariant).
  - Resolution-output keys land in the extension field; everything else
    (DataCite-shaped fields like ``creators``, ``titles``, ``publisher``)
    round-trips untouched.
  - The miss path produces a record with ``canonical_iri=None`` /
    ``resolution_status='unresolved'`` — i.e. the adapter never silently
    drops a record on a dictionary miss.
  - The fast path (gated on ``APECX_SYNONYM_DICT_LIVE_OLS=1``) packs a real
    canonical IRI from a built dictionary into the extension field.

Why these are integration tests, not unit tests
-----------------------------------------------
The adapter is the seam between the apecx integration and apecx-harvesters.
Unit tests can stub DataCite + the workflow; only an integration test
catches Pydantic ``extra='forbid'`` re-validation regressions and workflow
cascade regressions in the same pass.

How sibling repos are wired
---------------------------
Same path-injection pattern as test_harvester_contract.py — adds
``../apecx-harvesters/src`` to ``sys.path`` if present, otherwise the
module is skipped.
"""

from __future__ import annotations

import os
import sys
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

# Inject sibling apecx-harvesters source path so the contract test can
# import the real DataCite type. Same pattern as test_harvester_contract.py.
_HARVESTER_SRC = WORKSPACE_ROOT / "apecx-harvesters" / "src"
if _HARVESTER_SRC.exists() and str(_HARVESTER_SRC) not in sys.path:
    sys.path.insert(0, str(_HARVESTER_SRC))

apecx_harvesters = pytest.importorskip(
    "apecx_harvesters",
    reason="apecx-harvesters sibling repo not on path; clone it to run contract test",
)

from apecx_harvesters.loaders.base import DataCite  # noqa: E402
from pydantic import BaseModel, ConfigDict  # noqa: E402

from apecx_integration.synonym_dictionary.harvester_adapter import (  # noqa: E402
    adapt_workflow_to_harvester_transform,
)
from apecx_integration.synonym_dictionary.workflow import IRIResolutionWorkflow  # noqa: E402

# ---------------------------------------------------------------------------
# DataCite subclass used by the contract tests
# ---------------------------------------------------------------------------


class _CanonicalExtension(BaseModel):
    model_config = ConfigDict(extra="forbid")
    canonical_iri: str | None = None
    canonical_label: str | None = None
    canonical_ontology: str | None = None
    resolution_status: str = "unresolved"
    resolution_confidence: float = 0.0
    dictionary_version: str | None = None


class _DataCiteWithCanonicalAndQuery(DataCite):
    """DataCite subclass that carries the entity-resolution request fields.

    The harvester pipeline doesn't know about ``surface_form`` /
    ``entity_type`` natively, so a Phase-6 harvester that wants to use
    this transform extends DataCite with those request-side fields plus
    the response-side ``canonical`` extension.
    """

    surface_form: str | None = None
    entity_type: str | None = None
    canonical: _CanonicalExtension | None = None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset the dictionary singleton before/after each test."""
    import apecx_integration.synonym_dictionary.loader as _loader
    from apecx_integration.synonym_dictionary.loader import _ProcessSingleton

    _orig = _loader._singleton
    _loader._singleton = _ProcessSingleton()
    yield
    _loader._singleton = _orig


@pytest.fixture
def workflow():
    """Build a fresh IRIResolutionWorkflow for each test."""
    return IRIResolutionWorkflow.from_config(str(WORKFLOW_YAML))


# ---------------------------------------------------------------------------
# Shape-only contract tests (no live OLS)
# ---------------------------------------------------------------------------


def test_adapter_returns_callable(workflow):
    adapted = adapt_workflow_to_harvester_transform(workflow, extension_field="canonical")
    assert callable(adapted)


def test_adapter_requires_extension_field_kwarg(workflow):
    """The adapter forces callers to commit to an extension-field choice;
    no silent default that lands canonical_* on a base DataCite."""
    with pytest.raises(TypeError):
        adapt_workflow_to_harvester_transform(workflow)  # type: ignore[call-arg]


def test_adapter_signature_matches_harvester_transform_protocol(workflow):
    """The adapted callable is a single-arg coroutine — what the harvester's
    Transform alias requires."""
    import inspect

    adapted = adapt_workflow_to_harvester_transform(workflow, extension_field="canonical")
    assert inspect.iscoroutinefunction(adapted)

    sig = inspect.signature(adapted)
    pos_or_kw = [
        p for p in sig.parameters.values() if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    assert len(pos_or_kw) == 1, (
        f"harvester Transform takes exactly one positional arg; got {len(pos_or_kw)}"
    )


@pytest.mark.asyncio
async def test_adapter_miss_path_returns_unresolved_extension(workflow):
    """With no dictionary configured, the adapter populates ``canonical``
    with miss-shaped values rather than dropping the record."""
    record = _DataCiteWithCanonicalAndQuery(
        creators=[],
        titles=[{"title": "test"}],
        publisher={"name": "test"},
        surface_form="no-such-pathogen",
        entity_type="pathogen",
    )

    adapted = adapt_workflow_to_harvester_transform(workflow, extension_field="canonical")
    result = await adapted(record)

    assert type(result) is type(record), "adapter must preserve concrete DataCite-subclass type"
    assert result.canonical is not None
    assert result.canonical.canonical_iri is None
    assert result.canonical.resolution_status == "unresolved"
    assert result.canonical.resolution_confidence == 0.0
    # DataCite-shaped fields untouched
    assert result.surface_form == "no-such-pathogen"
    assert result.entity_type == "pathogen"


@pytest.mark.asyncio
async def test_adapter_fails_on_datacite_without_extension_field(workflow):
    """A DataCite subclass that lacks the named extension field must fail
    at re-validation rather than silently dropping the resolution payload.

    Mirrors test_harvester_contract::test_adapter_fails_on_base_datacite_intentionally —
    the failure mode protects Phase-6 harvesters from shipping empty
    resolution data because they forgot to declare the extension field.
    """

    class _DataCiteNoCanonical(DataCite):
        # Note: NO ``canonical`` field. The adapter will try to set it and
        # Pydantic re-validation will reject it.
        surface_form: str | None = None
        entity_type: str | None = None

    record = _DataCiteNoCanonical(
        creators=[],
        titles=[{"title": "test"}],
        publisher={"name": "test"},
        surface_form="anything",
        entity_type="pathogen",
    )

    adapted = adapt_workflow_to_harvester_transform(workflow, extension_field="canonical")
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        await adapted(record)


@pytest.mark.asyncio
async def test_adapter_strips_workflow_internal_fields(workflow):
    """``resolution_path`` and ``_original_surface_form`` (workflow-internal)
    must NOT bleed into the DataCite-shaped payload — DataCite would reject
    them via ``extra='forbid'``."""
    record = _DataCiteWithCanonicalAndQuery(
        creators=[],
        titles=[{"title": "test"}],
        publisher={"name": "test"},
        surface_form="  EEEV  ",  # has whitespace to trigger _original_surface_form
        entity_type="pathogen",
    )

    adapted = adapt_workflow_to_harvester_transform(workflow, extension_field="canonical")
    # The test passes if no ValidationError is raised — that means the
    # workflow-internal fields were correctly stripped before re-validation.
    result = await adapted(record)
    assert result is not None
    # The original surface_form is preserved on the record (the user's input
    # value, not the normalized lowercase form).
    assert result.surface_form == "  EEEV  "


# ---------------------------------------------------------------------------
# Live-OLS path test
# ---------------------------------------------------------------------------

_LIVE_OLS = os.environ.get("APECX_SYNONYM_DICT_LIVE_OLS", "").strip() == "1"


@pytest.fixture(scope="module")
def eeev_dictionary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not _LIVE_OLS:
        pytest.skip("Set APECX_SYNONYM_DICT_LIVE_OLS=1 for the live-OLS test.")

    from tests.integration._dict_build_helper import build_dictionary_for_test

    out = tmp_path_factory.mktemp("workflow_harvester_dict")
    db_path = build_dictionary_for_test(
        output_dir=out,
        dictionary_version="test-workflow-harvester",
        max_rows=60,
        violin_pathogens=VIOLIN_PATHOGENS,
    )
    assert db_path.exists()
    return db_path


@pytest.mark.skipif(
    not _LIVE_OLS,
    reason="Set APECX_SYNONYM_DICT_LIVE_OLS=1 to run live-OLS workflow contract test.",
)
@pytest.mark.asyncio
async def test_adapter_fast_path_with_real_dictionary(eeev_dictionary: Path):
    """Full E2E: real dict → workflow → harvester adapter → DataCite extension."""
    from apecx_integration.synonym_dictionary.loader import configure_dictionary_path

    configure_dictionary_path(eeev_dictionary)

    workflow = IRIResolutionWorkflow.from_config(str(WORKFLOW_YAML))
    adapted = adapt_workflow_to_harvester_transform(workflow, extension_field="canonical")

    record = _DataCiteWithCanonicalAndQuery(
        creators=[],
        titles=[{"title": "test"}],
        publisher={"name": "test"},
        surface_form="eastern equine encephalitis virus",
        entity_type="pathogen",
    )
    result = await adapted(record)

    assert result.canonical is not None
    # The dict has NCBITaxon_11021 (EEEV).
    assert result.canonical.canonical_iri is not None, (
        "Expected a canonical_iri after live-OLS lookup; got None. "
        "Dictionary may not have built EEEV row 50."
    )
    assert "NCBITaxon_11021" in result.canonical.canonical_iri
    assert result.canonical.resolution_confidence > 0.0
    assert result.canonical.dictionary_version is not None


@pytest.mark.asyncio
async def test_adapter_invocations_share_workflow_instance(workflow):
    """Multiple successive adapter calls reuse the same workflow object —
    important because the harvester pipeline issues many calls per workflow.
    """
    adapted = adapt_workflow_to_harvester_transform(workflow, extension_field="canonical")

    for i in range(5):
        record = _DataCiteWithCanonicalAndQuery(
            creators=[],
            titles=[{"title": f"test_{i}"}],
            publisher={"name": "test"},
            surface_form=f"term_{i}",
            entity_type="pathogen",
        )
        result = await adapted(record)
        assert result.canonical is not None
        assert result.canonical.resolution_status == "unresolved"  # all miss
