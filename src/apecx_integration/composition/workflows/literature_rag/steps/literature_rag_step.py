"""LiteratureRagStep — answer a query grounded in filtered literature (SKELETON STUB).

Second step of the literature_rag workflow. In the finished feature this step will run
retrieval-augmented generation over the upstream ontology-filtered record set and emit
an answer with citations. For now it is a DEGRADE-LOUD STUB: it logs a clear "not yet
implemented" line and returns a well-formed EMPTY envelope so the workflow wiring
(triggers + links + cascade) can be smoke-tested without any RAG or network dependency.

Deliberately imports NONE of the literature agents or the Globus client — those live in
other branches. Do not add such imports here until this stub is replaced.

Framework contract (nanobrain):
  - Created via ``from_config`` only (never a direct constructor).
  - Implements ``process``; NEVER overrides ``execute``.
  - Owns its input/output data units + trigger (declared in the sibling YAML).

The step has a single output data unit (``rag_output``). Per the framework's
single-output-fallback (``BaseStep._update_output_data_units``), the whole returned
dict is written into that unit, so it reaches the workflow-level output.
"""

from __future__ import annotations

from typing import Any

from nanobrain.core.step import BaseStep, StepConfig


class LiteratureRagStep(BaseStep):
    """Literature RAG answerer — skeleton stub returning an empty answer envelope."""

    COMPONENT_TYPE: str = "literature_rag_step"

    @classmethod
    def _get_config_class(cls):
        return StepConfig

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        self.nb_logger.warning(
            "literature_rag LiteratureRagStep: not yet implemented — "
            "returning empty answer envelope"
        )
        return {"answer": "", "citations": [], "status": "stub_not_implemented"}
