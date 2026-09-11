"""OntologyFilterStep — narrow a candidate record set by ontology terms (SKELETON STUB).

First step of the literature_rag workflow. In the finished feature this step will
filter candidate literature/records against an ontology-derived term set. For now it
is a DEGRADE-LOUD STUB: it logs a clear "not yet implemented" line and returns a
well-formed EMPTY envelope so the workflow wiring (triggers + links + cascade) can be
smoke-tested end to end without any retrieval or network dependency.

Deliberately imports NONE of the literature agents or the Globus client — those live
in other branches. Do not add such imports here until this stub is replaced.

Framework contract (nanobrain):
  - Created via ``from_config`` only (never a direct constructor).
  - Implements ``process``; NEVER overrides ``execute``.
  - Owns its input/output data units + trigger (declared in the sibling YAML).

The step has a single output data unit (``filter_output``). Per the framework's
single-output-fallback (``BaseStep._update_output_data_units``), the whole returned
dict is written into that unit, so the downstream link carries the full envelope.
"""

from __future__ import annotations

from typing import Any

from nanobrain.core.step import BaseStep, StepConfig


class OntologyFilterStep(BaseStep):
    """Ontology-driven record filter — skeleton stub returning an empty filtered set."""

    COMPONENT_TYPE: str = "ontology_filter_step"

    @classmethod
    def _get_config_class(cls):
        return StepConfig

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        self.nb_logger.warning(
            "literature_rag OntologyFilterStep: not yet implemented — returning empty filtered set"
        )
        return {"filtered_records": [], "status": "stub_not_implemented"}
