"""HarvestStampStep — the harvest front-end that makes literature_rag runnable
from a bare ``{organism, question}`` request.

The literature_rag cascade (resolve -> filter -> rag) expects the input envelope
to ALREADY carry ``stamped_records`` (PubMed records each tagged with an ``iris``
list). Nothing on the runnable surface produces those — the harvest+stamp lived
only in the evaluation harness (``agents/literature/ablation.py``). This step is
that missing front-end: given ``{organism, question}`` it harvests PubMed and
stamps each record with its NCBITaxon IRIs, emitting the exact
``{organism, question, stamped_records}`` envelope the existing workflow consumes.

It reuses, never reimplements:
  - ``_pubmed_helpers.build_focused_term`` — organism-anchored eSearch term
    (``("<organism>") AND (<concept> OR ...)``) so a topical question stays
    on-organism with high recall instead of ANDing every word to zero hits.
  - ``agents.literature.gazetteer.build_from_dictionary`` — the taxon tagger
    (same ``_is_taggable`` precision guard as the rest of the pipeline).
  - ``agents.literature.pipeline.harvest_and_stamp`` — harvest + stamp.

Degrade-loud:
  - Missing/empty ``organism`` or ``question`` → ``ValueError`` (a wiring bug —
    both are MCP-schema-required; not degradable data).
  - Dictionary unavailable → warn + an EMPTY gazetteer (records stamped with
    ``iris=[]``); the pipeline runs and the downstream filter honestly returns
    ``no_target_iri`` / drops to ``no_evidence`` rather than crashing.

Framework contract (nanobrain):
  - Created via ``from_config`` only; implements ``process``; never overrides
    ``execute``. Owns its input/output data units + trigger (sibling YAML).
  - Single output data unit (``harvest_output``); the whole returned envelope is
    written there via the framework's single-output fallback.

Input envelope (carried by the ``literature_rag_input`` data unit):
  - ``organism``: ``str`` — e.g. ``"Chikungunya virus"``.
  - ``question``: ``str`` — the scientist question.
  - ``max_papers``: ``int`` (optional, default 20) — PubMed harvest cap.
"""

from __future__ import annotations

import asyncio
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig


class HarvestStampStep(BaseStep):
    """PubMed harvest + taxon-IRI stamp — produces the ``stamped_records`` the
    literature_rag cascade expects from a bare ``{organism, question}`` request."""

    COMPONENT_TYPE: str = "harvest_stamp_step"

    #: The step's single input data-unit name (see sibling YAML). It is ALSO the
    #: workflow's first-step deposit point, so the MCP catalog entry's
    #: ``input_envelope_key`` MUST equal this.
    _INPUT_UNIT: str = "literature_rag_input"

    #: Default PubMed harvest cap when the request omits ``max_papers``.
    _DEFAULT_MAX_PAPERS: int = 20

    @classmethod
    def _get_config_class(cls):
        return StepConfig

    def _unwrap_envelope(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Return the request envelope.

        The workflow cascade delivers ``{"literature_rag_input": <envelope>}``
        (keyed by the input data-unit name); a direct ``process(<envelope>)`` call
        passes it flat. Accept both (mirrors the sibling steps' ``_unwrap_envelope``).
        """
        if (
            isinstance(input_data, dict)
            and set(input_data) == {self._INPUT_UNIT}
            and isinstance(input_data[self._INPUT_UNIT], dict)
        ):
            return input_data[self._INPUT_UNIT]
        return input_data if isinstance(input_data, dict) else {}

    def _build_gazetteer(self):
        """Build the taxon gazetteer from the synonym dictionary; degrade-loud to
        an EMPTY gazetteer (not a crash) when the dictionary is unavailable."""
        from apecx_integration.agents.literature.gazetteer import (
            build_from_dictionary,
            build_gazetteer,
        )

        try:
            from apecx_integration.synonym_dictionary.loader import default_dictionary_path

            return build_from_dictionary(str(default_dictionary_path()), entity_type="pathogen")
        except Exception as exc:  # noqa: BLE001 — degrade-loud, never crash the harvest
            self.nb_logger.warning(
                "literature_rag HarvestStampStep: gazetteer build failed (%s: %s); "
                "stamping with an EMPTY gazetteer (records get iris=[] → downstream "
                "filter degrades to no_evidence)",
                type(exc).__name__,
                exc,
            )
            return build_gazetteer({})

    def _harvest(self, term: str, gazetteer, max_papers: int) -> list[dict[str, Any]]:
        """Synchronous harvest + stamp (network + CPU-bound); offloaded via to_thread."""
        from apecx_integration.agents.literature.pipeline import harvest_and_stamp

        return harvest_and_stamp(term, gazetteer, max_papers=max_papers)

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        envelope = self._unwrap_envelope(input_data)
        organism = envelope.get("organism")
        question = envelope.get("question")

        if not isinstance(organism, str) or not organism.strip():
            raise ValueError(
                f"HarvestStampStep '{self.name}': envelope must carry a non-empty "
                f"'organism' string; got {type(organism).__name__}={organism!r}"
            )
        if not isinstance(question, str) or not question.strip():
            raise ValueError(
                f"HarvestStampStep '{self.name}': envelope must carry a non-empty "
                f"'question' string; got {type(question).__name__}={question!r}"
            )
        organism = organism.strip()
        question = question.strip()
        max_papers = int(envelope.get("max_papers") or self._DEFAULT_MAX_PAPERS)

        from apecx_integration.composition.steps._pubmed_helpers import build_focused_term

        # Organism-anchored, topic-OR-grouped term: on-organism AND high-recall.
        term = build_focused_term(f"{organism} {question}", owner_name=self.name)
        self.emit_progress("harvesting PubMed")
        gazetteer = self._build_gazetteer()
        try:
            stamped_records = await asyncio.to_thread(self._harvest, term, gazetteer, max_papers)
        except Exception as exc:  # noqa: BLE001 — network flake degrades to empty, never crash
            self.nb_logger.warning(
                "literature_rag HarvestStampStep: PubMed harvest failed (%s: %s); "
                "returning 0 stamped_records (downstream degrades to no_evidence)",
                type(exc).__name__,
                exc,
            )
            stamped_records = []

        self.nb_logger.info(
            "literature_rag HarvestStampStep: harvested %d PubMed record(s) for term %.80r",
            len(stamped_records),
            term,
        )
        return {"organism": organism, "question": question, "stamped_records": stamped_records}
