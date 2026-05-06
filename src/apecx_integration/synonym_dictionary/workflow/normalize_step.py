"""Step 1 of the IRI-resolution workflow — surface-form normalization.

Takes a batch of free-form entity records and rewrites each record's
``surface_form`` to a normalized comparison key (lowercased + stripped),
preserving the caller's original spelling under ``_original_surface_form``
so downstream consumers can render the user's wording back to a UI.

The ``entity_type`` string is *not* coerced to an :class:`EntityType`
enum here — the resolve step does that itself so the validation error
message points at the resolver, not at this normalizer.

Framework compliance
--------------------
- Subclasses :class:`BaseStep`, implements ``process()`` only.
- Step owns its input/output data units + trigger; the workflow owns the
  links into and out of those units.
"""

from __future__ import annotations

import logging
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig

log = logging.getLogger(__name__)


class NormalizeEntityRecordsStepConfig(StepConfig):
    """No extra fields beyond StepConfig — class-level constants suffice."""


class NormalizeEntityRecordsStep(BaseStep):
    """Normalize each record's ``surface_form`` to a lookup-friendly key.

    Expected ``process()`` input::

        {"entity_records": [
            {"surface_form": "  EEEV  ",
             "entity_type": "pathogen",
             "<other free fields>": "..."},
            ...
        ]}

    Return shape::

        {"normalized_records": [
            {"surface_form": "eeev",
             "_original_surface_form": "  EEEV  ",
             "entity_type": "pathogen",
             "<other free fields>": "..."},
            ...
        ]}

    Records whose ``surface_form`` is missing or non-string are passed
    through unchanged (with no ``_original_surface_form`` added) and an
    INFO log line is emitted — the resolve step is the single source of
    truth for shape validation, so we don't raise here.
    """

    COMPONENT_TYPE: str = "normalize_entity_records_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return NormalizeEntityRecordsStepConfig

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        entity_records = input_data.get("entity_records")
        if not isinstance(entity_records, list):
            raise ValueError(
                f"NormalizeEntityRecordsStep '{self.name}': input_data must "
                f"have 'entity_records' as a list, got "
                f"{type(entity_records).__name__}"
            )

        normalized: list[dict[str, Any]] = []
        for record in entity_records:
            if not isinstance(record, dict):
                raise ValueError(
                    f"NormalizeEntityRecordsStep '{self.name}': each entity "
                    f"record must be a dict, got {type(record).__name__}"
                )
            new_record = dict(record)
            surface_form = new_record.get("surface_form")
            if isinstance(surface_form, str):
                new_record["_original_surface_form"] = surface_form
                new_record["surface_form"] = surface_form.strip().lower()
            else:
                log.info(
                    "NormalizeEntityRecordsStep %s: record missing string "
                    "'surface_form' (got %s); passing through unchanged.",
                    self.name,
                    type(surface_form).__name__,
                )
            normalized.append(new_record)

        log.info(
            "NormalizeEntityRecordsStep %s: normalized %d record(s).",
            self.name,
            len(normalized),
        )
        return {"normalized_records": normalized}
