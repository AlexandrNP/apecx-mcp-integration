"""Contract test — synonym-dictionary transform composes with apecx-harvesters.

This is the load-bearing Phase-1 insurance against integration debt at
Phase 6.  Per the v5 plan
(``ontology_integration_initial_analysis.md`` §0.2, §6.1), the synonym
dictionary is being developed local-first with apecx-harvesters
integration deferred.  This test verifies that an
:class:`EntityResolutionTransform` can be wrapped via
:func:`adapt_to_harvester_transform` to satisfy the harvester's
``Transform = Callable[[DataCite], Awaitable[DataCite]]`` shape.

Without this test, "we'll lift it to apecx-harvesters later" is a
hopeful claim.  With this test, it's a verified one — at least at the
protocol level.

Caveats this test does NOT cover (see harvester_adapter.py for the full
list):

- Whether DataCite is the right carrier for entity-table rows at all
  (Phase 6 ADR question).
- Whether the harvester's pipeline runtime semantics (logging, retry,
  observability) impose constraints beyond the function signature.
- Whether any Phase 6 changes to apecx-harvesters break this contract.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# apecx-harvesters lives as a sibling repo in the workspace; it isn't
# pip-installed into our venv.  Add its source path so the contract test
# can import the real types.  This mirrors what Phase 6 would do at
# integration time, except via PYTHONPATH instead of a real install.
_HARVESTER_SRC = Path(__file__).resolve().parents[2].parent / "apecx-harvesters" / "src"
if _HARVESTER_SRC.exists() and str(_HARVESTER_SRC) not in sys.path:
    sys.path.insert(0, str(_HARVESTER_SRC))


# Skip the entire module if apecx-harvesters isn't importable — keeps CI
# robust on machines that don't have the sibling repo cloned.
apecx_harvesters = pytest.importorskip(
    "apecx_harvesters",
    reason="apecx-harvesters sibling repo not on path; install/clone it to run contract test",
)

from apecx_harvesters.loaders.base import DataCite  # noqa: E402
from apecx_harvesters.pipeline.run import Transform  # noqa: E402
from apecx_integration.synonym_dictionary.harvester_adapter import (  # noqa: E402
    adapt_to_harvester_transform,
)
from apecx_integration.synonym_dictionary.transform import (  # noqa: E402
    EntityRecord,
    EntityResolutionTransform,
)
from pydantic import BaseModel, ConfigDict  # noqa: E402

# ---------- a trivial entity transform we can adapt ----------


async def _stub_entity_transform(record: EntityRecord) -> EntityRecord:
    """Adds canonical_iri / status to whatever we get.  Mirrors the shape
    that Stage 2's resolver will produce."""
    return {
        **record,
        "canonical_iri": "http://purl.obolibrary.org/obo/NCBITaxon_37124",
        "canonical_label": "Chikungunya virus",
        "canonical_ontology": "ncbitaxon",
        "resolution_status": "id_anchored",
        "resolution_confidence": 1.0,
        "dictionary_version": "test-v1",
    }


# ---------- DataCite subclass simulating a Phase-6 harvester extension ----------
#
# DataCite uses ``extra='forbid'`` so canonical_* keys cannot be added to a
# base DataCite directly.  The harvester's documented pattern (see
# ``apecx-harvesters/CLAUDE.md``) is to extend DataCite via subclassing
# with a domain-specific nested field.  This sample subclass mirrors what
# a Phase-6 harvester would supply.


class _CanonicalExtension(BaseModel):
    model_config = ConfigDict(extra="forbid")
    canonical_iri: str | None = None
    canonical_label: str | None = None
    canonical_ontology: str | None = None
    resolution_status: str = "unresolved"
    resolution_confidence: float = 0.0
    dictionary_version: str | None = None


class _DataCiteWithCanonical(DataCite):
    canonical: _CanonicalExtension | None = None


# ---------- the contract assertions ----------


def test_entity_transform_satisfies_callable_protocol() -> None:
    """Sanity: our EntityResolutionTransform alias is the type we say it is."""
    fn: EntityResolutionTransform = _stub_entity_transform
    assert callable(fn)


def test_adapter_returns_callable() -> None:
    adapted = adapt_to_harvester_transform(_stub_entity_transform, extension_field="canonical")
    assert callable(adapted)


def test_adapter_requires_extension_field_kwarg() -> None:
    """The adapter forces callers to commit to an extension-field choice;
    no silent default that lands canonical_* on a base DataCite (which
    would fail at re-validation due to extra='forbid')."""
    with pytest.raises(TypeError):
        # Missing required keyword-only ``extension_field``
        adapt_to_harvester_transform(_stub_entity_transform)  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_adapter_preserves_datacite_subclass_and_fills_extension() -> None:
    """The full round-trip: adapter dumps DataCite -> dict, runs entity
    transform, packs resolution fields into the named extension field,
    re-validates as the original concrete type."""
    record = _DataCiteWithCanonical(
        creators=[],
        titles=[{"title": "test"}],
        publisher={"name": "test"},
    )

    adapted = adapt_to_harvester_transform(_stub_entity_transform, extension_field="canonical")
    result = await adapted(record)

    # Type preservation — Phase-6 subclasses must round-trip.
    assert type(result) is type(record), (
        f"adapter must preserve concrete DataCite type; "
        f"got {type(result).__name__}, expected {type(record).__name__}"
    )

    # Extension field populated from the resolution payload.
    assert result.canonical is not None
    assert result.canonical.canonical_iri == ("http://purl.obolibrary.org/obo/NCBITaxon_37124")
    assert result.canonical.resolution_status == "id_anchored"
    assert result.canonical.resolution_confidence == 1.0


@pytest.mark.asyncio
async def test_adapter_fails_on_base_datacite_intentionally() -> None:
    """Base DataCite has no ``canonical`` field; adapter MUST fail loudly
    rather than silently dropping the resolution payload.  This protects
    Phase-6 harvesters that forget to subclass DataCite from shipping
    silently-empty resolution data."""
    record = DataCite(
        creators=[],
        titles=[{"title": "test"}],
        publisher={"name": "test"},
    )

    adapted = adapt_to_harvester_transform(_stub_entity_transform, extension_field="canonical")
    # Pydantic raises ValidationError because ``canonical`` is not a
    # valid field on plain DataCite (extra='forbid').
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        await adapted(record)


def test_adapter_signature_matches_harvester_transform_protocol() -> None:
    """Verify the adapted callable's runtime type satisfies the harvester's
    ``Transform`` alias structurally.  ``Transform`` is a
    ``Callable[[DataCite], Awaitable[DataCite]]`` — we can't enforce
    parameterized callable typing at runtime, but we can verify the
    adapted function is a coroutine-returning callable of one positional
    argument, which is what the harvester pipeline expects."""
    import inspect

    adapted = adapt_to_harvester_transform(_stub_entity_transform, extension_field="canonical")
    assert inspect.iscoroutinefunction(adapted), (
        "harvester pipeline expects async transforms; adapter must return "
        "a coroutine-returning callable"
    )

    sig = inspect.signature(adapted)
    pos_or_kw = [
        p for p in sig.parameters.values() if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    assert len(pos_or_kw) == 1, (
        f"harvester Transform takes exactly one positional argument; "
        f"adapter has {len(pos_or_kw)}"
    )

    # The Transform alias is referenced for type-narrowing only; assert
    # its presence so a Phase 6 rename surfaces in this test.
    assert Transform is not None
