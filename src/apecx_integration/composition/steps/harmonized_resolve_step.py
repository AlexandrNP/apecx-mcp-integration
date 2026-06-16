"""HarmonizedResolveStep — resolve a biomedical term to a canonical IRI + classify HITL state.

First step of the harmonized_search workflow. Wraps
``apecx_integration.synonym_dictionary.lookup_entity`` in a nanobrain
``BaseStep`` so it can be linked into the EnvelopeStep / decomposition
infrastructure (EO-10/12/13).

Input contract (dict reaching ``process()`` after the framework unwraps
the trigger envelope ``{du_name: payload}``):

- ``term``: ``str`` (required) — the user surface form to resolve. Can be
  a free-text token (``"CHIKV"``), a verbose species name
  (``"Chikungunya virus"``), or a canonical IRI
  (``"http://purl.obolibrary.org/obo/NCBITaxon_37124"``).
- ``index``: ``str`` (required) — short name of the target Globus index
  (e.g. ``"bvbrc_genome"``). The resolution itself doesn't depend on the
  index, but downstream steps do, so we carry it through.

Output (under data unit ``plan``):

- ``term``: echoed.
- ``index``: echoed.
- ``resolution_path``: ``"fast" | "ambiguous" | "ancestor" | "fuzzy" | "deleted" | "miss"``.
- ``canonical_iri``: the resolved IRI, or ``None`` for ambiguous / miss.
- ``canonical_label``: ``str | None``.
- ``canonical_ontology``: ``str | None``.
- ``confidence``: ``float`` (0.0 - 1.0).
- ``resolution_status``: the underlying ``ResolutionStatus`` enum value.
- ``synonyms``: ``list[str]`` of surface forms for the canonical entity.
- ``candidates``: ``list[dict]`` of ambiguous candidates (one entry each
  with ``canonical_iri / canonical_label / canonical_ontology / confidence``).
  Empty list for non-ambiguous resolutions.
- ``needs_disambiguation``: ``bool`` — True iff ``resolution_path == "ambiguous"``.
  Downstream steps gate on this to decide whether to run Globus queries
  or emit a paused envelope.
- ``evidence``: free-text reasoning string from the resolver.

The step never raises on a miss — a miss produces a structured output
with ``resolution_path = "miss"`` so the workflow can still emit a
WorkflowResult envelope explaining why.

Compliance notes:
- ``from_config``-only construction; subclass of ``BaseStep``.
- Implements ``process()``; does NOT override ``execute()``.
- The synonym dictionary path is resolved from the
  ``APECX_SYNONYM_DICT_PATH`` env var at import time (same convention
  the ``resolve_canonical_entity`` MCP tool uses).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig

from apecx_integration.synonym_dictionary.enums import EntityType
from apecx_integration.synonym_dictionary.loader import (
    configure_dictionary_path,
    get_dictionary_index,
)
from apecx_integration.synonym_dictionary.lookup import LookupResult, lookup_entity
from apecx_integration.synonym_dictionary.schema import DictionaryEntry

log = logging.getLogger(__name__)

_INPUT_DU = "resolve_input"
_OUTPUT_KEY = "plan"


# Resolve the dictionary path from the env var at import time. The
# resolve_canonical_entity tool uses the same pattern; we mirror it so
# both surfaces hit the same dict.
_dict_path_env = os.environ.get("APECX_SYNONYM_DICT_PATH")
if _dict_path_env:
    configure_dictionary_path(Path(_dict_path_env))


_ENTITY_TYPE_MAP: dict[str, EntityType] = {
    "pathogen": EntityType.PATHOGEN,
    "vaccine": EntityType.VACCINE,
    "disease": EntityType.DISEASE,
    "gene": EntityType.GENE,
}


def build_resolution_plan(term: str, index: str, entity_type_str: str = "") -> dict[str, Any]:
    """Resolve ``term`` to a canonical entity + classify HITL state, returning the
    plain ``plan`` dict (NOT wrapped under any output data-unit key).

    Factored out of :meth:`HarmonizedResolveStep.process` so other steps
    (e.g. ``EpitopeResolveStep``) can produce the same plan shape without
    re-implementing the lookup + ambiguity-detection logic. The plan dict
    carries the keys: ``term``, ``index``, ``resolution_path``,
    ``canonical_iri``, ``canonical_label``, ``canonical_ontology``,
    ``confidence``, ``resolution_status``, ``synonyms``, ``candidates``,
    ``needs_disambiguation``, ``evidence``.

    ``index`` is echoed through unchanged — the resolution itself does not
    depend on the index, but downstream steps do, so it rides along.
    Raises ``ValueError`` on an unknown ``entity_type_str``.
    """
    entity_type: EntityType | None = None
    if entity_type_str:
        if entity_type_str not in _ENTITY_TYPE_MAP:
            raise ValueError(
                f"build_resolution_plan: unknown entity_type={entity_type_str!r}; "
                f"expected one of {sorted(_ENTITY_TYPE_MAP)} or empty for any-type."
            )
        entity_type = _ENTITY_TYPE_MAP[entity_type_str]

    result: LookupResult = lookup_entity(term, entity_type=entity_type)

    # Ambiguity detection: the eo-mvp branch's LookupResult is the
    # "first match wins" shape; ambiguity requires a separate query
    # against the dictionary index. If multiple distinct canonical
    # IRIs match this surface form, we override the resolver's
    # single-result optimism and emit an ambiguous plan.
    candidates: list[dict[str, Any]] = []
    resolution_path: str = result.path
    canonical_iri: str | None = result.canonical_iri
    canonical_label: str | None = result.canonical_label
    canonical_ontology: str | None = result.canonical_ontology

    # Only run the multi-candidate check for non-IRI surface inputs
    # (an IRI input is by construction unambiguous — it's already a
    # canonical identifier).
    is_iri_input = term.startswith(("http://", "https://"))
    if not is_iri_input:
        try:
            index_obj, _err = get_dictionary_index()
            if index_obj is not None:
                # Two ambiguity-detection paths, in order of trust:
                # (a) The dictionary's curated ambiguous_surface_forms
                #     table records (winning, alternative) IRI pairs
                #     captured at build time. This is the
                #     authoritative source for already-known
                #     conflicts (RSV → 6 candidates, HEV, BVDV,
                #     etc.).
                # (b) Otherwise fall through to ``lookup_any_type``
                #     which surfaces multi-IRI conflicts the build
                #     pass may have missed.
                surface_norm = " ".join(term.casefold().split())
                amb_rows = index_obj.lookup_ambiguous_surface_forms(
                    surface_form=surface_norm,
                    limit=50,
                )
                candidate_iris: list[str] = []
                seen_iris: set[str] = set()
                for row in amb_rows:
                    for iri_key in ("winning_canonical_iri", "alternative_canonical_iri"):
                        iri = row.get(iri_key)
                        if iri and iri not in seen_iris:
                            seen_iris.add(iri)
                            candidate_iris.append(iri)
                if not candidate_iris:
                    matches: list[DictionaryEntry] = index_obj.lookup_any_type(term)
                    for entry in matches:
                        if entry.canonical_iri not in seen_iris:
                            seen_iris.add(entry.canonical_iri)
                            candidate_iris.append(entry.canonical_iri)

                if len(candidate_iris) > 1:
                    resolution_path = "ambiguous"
                    canonical_iri = None
                    canonical_label = None
                    canonical_ontology = None
                    for iri in candidate_iris:
                        entry = index_obj.lookup_by_iri(iri)
                        if entry is not None:
                            candidates.append(
                                {
                                    "canonical_iri": entry.canonical_iri,
                                    "canonical_label": entry.canonical_label,
                                    "canonical_ontology": entry.ontology.value,
                                    "confidence": entry.confidence,
                                }
                            )
                        else:
                            candidates.append(
                                {
                                    "canonical_iri": iri,
                                    "canonical_label": None,
                                    "canonical_ontology": None,
                                    "confidence": 0.0,
                                }
                            )
                    log.info(
                        "build_resolution_plan: detected %d-way ambiguity for "
                        "term=%r — overriding path to 'ambiguous'",
                        len(candidates),
                        term,
                    )
        except Exception as exc:  # noqa: BLE001
            # Multi-candidate detection is best-effort. If the dict
            # is unavailable we keep the resolver's optimistic
            # single answer rather than failing the whole step.
            log.warning(
                "build_resolution_plan: multi-candidate detection failed "
                "(%s); keeping single-match resolution",
                exc,
            )

    plan: dict[str, Any] = {
        "term": term,
        "index": index,
        "resolution_path": resolution_path,
        "canonical_iri": canonical_iri,
        "canonical_label": canonical_label,
        "canonical_ontology": canonical_ontology,
        "confidence": result.confidence if resolution_path != "ambiguous" else 0.0,
        "resolution_status": result.resolution_status.value,
        "synonyms": list(result.synonyms) if resolution_path != "ambiguous" else [],
        "candidates": candidates,
        "needs_disambiguation": resolution_path == "ambiguous",
        "evidence": result.evidence,
    }

    log.info(
        "build_resolution_plan: term=%r index=%r path=%s candidates=%d",
        term,
        index,
        result.path,
        len(candidates),
    )

    return plan


class HarmonizedResolveStep(BaseStep):
    """Resolve term → canonical IRI + classify HITL state for downstream gating."""

    @classmethod
    def _get_config_class(cls):
        return StepConfig

    async def process(self, input_data: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"HarmonizedResolveStep '{self.name}': input_data must be a "
                f"dict, got {type(input_data).__name__}"
            )

        # Unwrap framework trigger envelope ({du_name: payload}) if needed.
        if (
            _INPUT_DU in input_data
            and isinstance(input_data[_INPUT_DU], dict)
            and "term" not in input_data
        ):
            input_data = input_data[_INPUT_DU]

        term = input_data.get("term")
        if not isinstance(term, str) or not term.strip():
            raise ValueError(
                f"HarmonizedResolveStep '{self.name}': input must carry a "
                f"non-empty 'term' string; got "
                f"{type(term).__name__}={term!r}"
            )

        index = input_data.get("index")
        if not isinstance(index, str) or not index.strip():
            raise ValueError(
                f"HarmonizedResolveStep '{self.name}': input must carry a "
                f"non-empty 'index' string; got "
                f"{type(index).__name__}={index!r}"
            )

        entity_type_str = input_data.get("entity_type") or ""
        return {_OUTPUT_KEY: build_resolution_plan(term, index, entity_type_str)}
