"""OntologyFilterStep — narrow a stamped record set to one NCBITaxon IRI.

First step of the literature_rag workflow. This is the TRUE NCBITaxon-IRI filter
(deliverable-2 stage 1): it keeps only the input records that were stamped with a
given target NCBITaxon PURL, and drops the rest.

Filtering is PURE over the provided input. Organism-name -> NCBITaxon-IRI
RESOLUTION (gazetteer / dictionary lookup) is a SEPARATE upstream concern — this
step neither resolves names nor stamps records; it filters records that already
carry an ``iris: list[str]`` field against a ``target_iri`` supplied in the same
envelope. Deliberately imports NONE of the gazetteer, the Globus client, or any
dictionary — it does no network or disk I/O.

Framework contract (nanobrain):
  - Created via ``from_config`` only (never a direct constructor).
  - Implements ``process``; NEVER overrides ``execute``.
  - Owns its input/output data units + trigger (declared in the sibling YAML).

Input envelope (carried by the ``filter_input`` data unit):
  - ``stamped_records``: ``list[dict]`` — each record already stamped with an
    ``iris: list[str]`` field. Missing/None is treated as ``[]``.
  - ``target_iri``: ``str`` — a full NCBITaxon PURL, e.g.
    ``http://purl.obolibrary.org/obo/NCBITaxon_37124``.

Output envelope (written to the single ``filter_output`` data unit):
  - ``filtered_records``: records whose ``iris`` contain ``target_iri``.
  - ``target_iri``: the IRI that was filtered on (None when absent).
  - ``status``: ``"ok"`` on a real filter, ``"no_target_iri"`` when degraded.

Degrade-loud: a missing/empty ``target_iri`` is a real caller error (the upstream
resolution stage produced nothing) — we log a clear WARNING and return an empty,
well-formed envelope with ``status="no_target_iri"`` rather than silently keeping
or dropping everything.
"""

from __future__ import annotations

from typing import Any

from nanobrain.core.step import BaseStep, StepConfig


class OntologyFilterStep(BaseStep):
    """NCBITaxon-IRI record filter — keeps records stamped with a target IRI."""

    COMPONENT_TYPE: str = "ontology_filter_step"

    #: The step's single input data-unit name (see sibling YAML).
    _INPUT_UNIT: str = "filter_input"

    @classmethod
    def _get_config_class(cls):
        return StepConfig

    def _unwrap_envelope(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Return the filter envelope.

        The workflow cascade delivers ``{"filter_input": <envelope>}`` (keyed by
        the input data-unit name), whereas a direct ``process(<envelope>)`` call
        passes the envelope flat. Accept both: unwrap the single-key DU form,
        otherwise use the dict as-is.
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
        target_iri = envelope.get("target_iri")
        stamped_records = envelope.get("stamped_records") or []

        if not target_iri:
            self.nb_logger.warning(
                "literature_rag OntologyFilterStep: missing/empty target_iri — "
                "cannot filter (organism->IRI resolution is an upstream concern); "
                "returning empty filtered set with status=no_target_iri"
            )
            return {"filtered_records": [], "target_iri": None, "status": "no_target_iri"}

        filtered_records = [r for r in stamped_records if target_iri in (r.get("iris") or [])]
        self.nb_logger.info(
            "literature_rag OntologyFilterStep: filtered %d/%d records on %s",
            len(filtered_records),
            len(stamped_records),
            target_iri,
        )
        return {
            "filtered_records": filtered_records,
            "target_iri": target_iri,
            "status": "ok",
        }
