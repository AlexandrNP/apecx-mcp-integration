"""Step 2 of the IRI-resolution workflow — canonical IRI lookup.

Wraps :func:`apecx_integration.synonym_dictionary.lookup.lookup_entity`
as a nanobrain step. For each normalized record, calls the lookup and
translates the resulting :class:`LookupResult` into the canonical-resolution
keys that the rest of APECx (and the harvester adapter) expect:

- ``canonical_iri``
- ``canonical_label``
- ``canonical_ontology``
- ``resolution_status``     — the :class:`ResolutionStatus` string value
- ``resolution_confidence``
- ``dictionary_version``    — pulled out of ``LookupResult.evidence``
- ``resolution_path``       — ``"fast" | "ancestor" | "slow" | "miss"``

Why ``resolution_path`` is added on top of :data:`RESOLUTION_OUTPUT_KEYS`
-------------------------------------------------------------------------
The harvester contract is
:data:`apecx_integration.synonym_dictionary.transform.RESOLUTION_OUTPUT_KEYS`
(six keys, no path). The path is a Stage-2 visibility hint; it survives
through the workflow but the harvester adapter strips it out (or routes
it to the extension field) before re-validating against DataCite. See
``harvester_adapter.py``'s docstring on the ``extra='forbid'`` constraint.

Framework compliance
--------------------
- Subclasses :class:`BaseStep`, implements ``process()`` only.
- Optional ``dictionary_path`` config field calls
  :func:`configure_dictionary_path` at step init so a self-contained test
  can wire a fixture dictionary without setting ``APECX_SYNONYM_DICT_PATH``.
- Step owns its input/output data units + trigger; the workflow owns links.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import Field

from apecx_integration.synonym_dictionary.enums import EntityType
from apecx_integration.synonym_dictionary.loader import configure_dictionary_path
from apecx_integration.synonym_dictionary.lookup import LookupResult, lookup_entity

log = logging.getLogger(__name__)


# String → EntityType. Unknown strings are silently dropped (entity_type=None,
# which makes lookup_entity search across all types). Done as a dict so the
# resolve step is self-documenting about which strings are recognized.
_ENTITY_TYPE_MAP: dict[str, EntityType] = {
    "pathogen": EntityType.PATHOGEN,
    "vaccine": EntityType.VACCINE,
    "disease": EntityType.DISEASE,
    "gene": EntityType.GENE,
    "genome": EntityType.GENOME,
}

# Regex for pulling the dictionary_version out of LookupResult.evidence.
# The lookup module's ``_entry_to_result`` builds it as
# ``"dictionary_version=<x>; source_records=..."``.
_DICTIONARY_VERSION_RE = re.compile(r"dictionary_version=([^;]+)")


class ResolveIRIStepConfig(StepConfig):
    """Config for :class:`ResolveIRIStep`.

    ``dictionary_path``: optional path to a SQLite dictionary artifact.
    When set, the step calls
    :func:`apecx_integration.synonym_dictionary.loader.configure_dictionary_path`
    at init so callers can wire a fixture artifact without relying on
    the ``APECX_SYNONYM_DICT_PATH`` env var. When unset (or empty), the
    step relies on whatever the process-level singleton has already been
    pointed at — typically by the MCP server at startup.
    """

    dictionary_path: str | None = Field(
        default=None,
        description=(
            "Optional path to the SQLite dictionary artifact. When set, the "
            "step configures the process-level dictionary singleton at init."
        ),
    )


class ResolveIRIStep(BaseStep):
    """Look up each normalized record's surface form against the synonym
    dictionary and write the canonical-IRI fields onto the record.

    Expected ``process()`` input::

        {"normalized_records": [
            {"surface_form": "eeev",
             "entity_type": "pathogen",
             "_original_surface_form": "EEEV",
             ...},
            ...
        ]}

    Return shape::

        {"resolved_records": [
            {"surface_form": "eeev",
             "entity_type": "pathogen",
             "_original_surface_form": "EEEV",
             "canonical_iri": "http://purl.obolibrary.org/obo/NCBITaxon_11036",
             "canonical_label": "Eastern equine encephalitis virus",
             "canonical_ontology": "ncbitaxon",
             "resolution_status": "id_anchored",
             "resolution_confidence": 1.0,
             "dictionary_version": "2025.10",
             "resolution_path": "fast",
             ...},
            ...
        ]}

    Records with non-string ``surface_form`` are passed through untouched
    (matching :class:`NormalizeEntityRecordsStep`'s lenient handling).
    Records whose ``entity_type`` string isn't in :data:`_ENTITY_TYPE_MAP`
    are looked up *across all types* (lookup_entity's default when the
    enum hint is None).
    """

    COMPONENT_TYPE: str = "resolve_iri_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return ResolveIRIStepConfig

    @classmethod
    def extract_component_config(cls, config: ResolveIRIStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "dictionary_path": getattr(config, "dictionary_path", None),
        }

    def _init_from_config(
        self,
        config: ResolveIRIStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._dictionary_path: str | None = component_config.get("dictionary_path")
        # Empty string is treated as "not set" — common when the value
        # comes from ``${APECX_SYNONYM_DICT_PATH:-}`` interpolation and
        # the env var isn't exported.
        if self._dictionary_path:
            configure_dictionary_path(self._dictionary_path)
            log.info(
                "ResolveIRIStep %s: configured dictionary path %s",
                self.name,
                self._dictionary_path,
            )

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        normalized_records = input_data.get("normalized_records")
        if not isinstance(normalized_records, list):
            raise ValueError(
                f"ResolveIRIStep '{self.name}': input_data must have "
                f"'normalized_records' as a list, got "
                f"{type(normalized_records).__name__}"
            )

        resolved: list[dict[str, Any]] = []
        miss_count = 0
        for record in normalized_records:
            if not isinstance(record, dict):
                raise ValueError(
                    f"ResolveIRIStep '{self.name}': each record must be a "
                    f"dict, got {type(record).__name__}"
                )
            new_record = dict(record)
            surface_form = new_record.get("surface_form")
            if not isinstance(surface_form, str):
                # Mirror normalize-step's lenient passthrough.
                log.info(
                    "ResolveIRIStep %s: record missing string 'surface_form' "
                    "(got %s); skipping lookup.",
                    self.name,
                    type(surface_form).__name__,
                )
                resolved.append(new_record)
                continue

            entity_type_str = new_record.get("entity_type")
            entity_type_enum = (
                _ENTITY_TYPE_MAP.get(entity_type_str) if isinstance(entity_type_str, str) else None
            )

            result = lookup_entity(surface_form, entity_type=entity_type_enum)
            self._merge_result_into_record(new_record, result)
            if result.path == "miss":
                miss_count += 1
            resolved.append(new_record)

        log.info(
            "ResolveIRIStep %s: resolved %d record(s); %d miss(es).",
            self.name,
            len(resolved),
            miss_count,
        )
        return {"resolved_records": resolved}

    @staticmethod
    def _merge_result_into_record(record: dict[str, Any], result: LookupResult) -> None:
        """Write the LookupResult fields onto ``record`` using the
        :data:`RESOLUTION_OUTPUT_KEYS`-aligned shape.
        """
        record["canonical_iri"] = result.canonical_iri
        record["canonical_label"] = result.canonical_label
        record["canonical_ontology"] = result.canonical_ontology
        # ResolutionStatus is a StrEnum — its string value is the wire form.
        record["resolution_status"] = result.resolution_status.value
        record["resolution_confidence"] = result.confidence
        record["dictionary_version"] = _extract_dictionary_version(result.evidence)
        record["resolution_path"] = result.path


def _extract_dictionary_version(evidence: str) -> str | None:
    """Pull ``dictionary_version=<x>`` out of LookupResult.evidence.

    Returns None when the substring isn't present (slow-path / miss /
    ancestor with custom evidence string formats).
    """
    match = _DICTIONARY_VERSION_RE.search(evidence or "")
    if match is None:
        return None
    return match.group(1).strip()
