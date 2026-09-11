"""ResolveOrganismStep — enrich the envelope with a target NCBITaxon IRI.

First step of the literature_rag cascade. It turns the free-text ``organism``
term carried by the input envelope into the canonical NCBITaxon PURL IRI that the
downstream OntologyFilterStep keys on, and passes the WHOLE envelope through
enriched with that ``target_iri`` so later steps still see ``question``,
``stamped_records`` and everything else.

Resolution is delegated to the shared, reuse-not-reimplement resolver
``apecx_integration.agents.literature.resolve.resolve_organism_to_iri`` (the SAME
build-time normalizer + dictionary ``inverse_index`` read the index is built
from), so runtime keys agree with the index.

Degrade-loud: an unresolvable organism (or a missing/empty ``organism`` field)
yields ``target_iri=None``. We do NOT raise — the downstream OntologyFilterStep
already degrades loud on a missing ``target_iri`` (status=no_target_iri), so a
miss surfaces as an honest empty answer, not a crash. A clear WARNING is logged.

Framework contract (nanobrain):
  - Created via ``from_config`` only (never a direct constructor).
  - Implements ``process``; NEVER overrides ``execute``.
  - Owns its input/output data units + trigger (declared in the sibling YAML).
  - Single output data unit (``resolve_output``); the whole returned enriched
    envelope is written there via the framework's single-output fallback.

Input envelope (carried by the ``resolve_input`` data unit):
  - ``organism``: ``str`` — free-text organism name, e.g. ``"Chikungunya virus"``.
  - plus any other fields (``question``, ``stamped_records``, ...) — passed through.

Output envelope (written to the single ``resolve_output`` data unit):
  - ``{**envelope, "target_iri": <NCBITaxon PURL | None>}``.
"""

from __future__ import annotations

from typing import Any

from nanobrain.core.step import BaseStep, StepConfig

from apecx_integration.agents.literature.resolve import resolve_organism_to_iri


class ResolveOrganismStep(BaseStep):
    """Organism-name -> NCBITaxon-IRI enricher — adds ``target_iri`` to the envelope."""

    COMPONENT_TYPE: str = "resolve_organism_step"

    #: The step's single input data-unit name (see sibling YAML).
    _INPUT_UNIT: str = "resolve_input"

    @classmethod
    def _get_config_class(cls):
        return StepConfig

    def _unwrap_envelope(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Return the enrichment envelope.

        The workflow cascade delivers ``{"resolve_input": <envelope>}`` (keyed by
        the input data-unit name), whereas a direct ``process(<envelope>)`` call
        passes the envelope flat. Accept both (mirrors
        ``OntologyFilterStep._unwrap_envelope``).
        """
        if (
            isinstance(input_data, dict)
            and set(input_data) == {self._INPUT_UNIT}
            and isinstance(input_data[self._INPUT_UNIT], dict)
        ):
            return input_data[self._INPUT_UNIT]
        return input_data if isinstance(input_data, dict) else {}

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        envelope = self._unwrap_envelope(input_data)
        organism = envelope.get("organism")

        if not isinstance(organism, str) or not organism.strip():
            self.nb_logger.warning(
                "literature_rag ResolveOrganismStep: missing/empty 'organism' — "
                "cannot resolve an NCBITaxon IRI; enriching with target_iri=None "
                "(downstream filter degrades loud with status=no_target_iri)"
            )
            return {**envelope, "target_iri": None}

        target_iri = resolve_organism_to_iri(organism.strip())
        if not target_iri:
            self.nb_logger.warning(
                "literature_rag ResolveOrganismStep: no NCBITaxon IRI for organism "
                "%r; enriching with target_iri=None (downstream filter degrades "
                "loud with status=no_target_iri)",
                organism,
            )
            return {**envelope, "target_iri": None}

        self.nb_logger.info(
            "literature_rag ResolveOrganismStep: resolved organism %r -> %s",
            organism,
            target_iri,
        )
        return {**envelope, "target_iri": target_iri}
