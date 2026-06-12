"""EnvelopeStep — wrap a workflow step's output into a standard WorkflowResult (EO-13).

A reusable terminal step: place it last in a workflow so the workflow's output is always a
``WorkflowResult`` (markdown + optional handle). The external orchestrating LLM then sees a
uniform shape regardless of which workflow ran.

Input contract (the dict reaching ``process()`` after the framework's ``{du_name: payload}``
unwrap) — the step expects a dict that MAY carry:

- ``markdown``: ``str`` — the presentation text (required; loud error if absent/empty).
- ``data``: ``dict`` — a serialized ``DataShape`` to stash behind a handle. When present it
  is parsed (loud on bad ``kind``), stored via the handle store, and its handle + preview
  are attached. The structured payload is thereby kept OUT of the markdown channel — the
  whole point of the two-channel envelope.
- ``run_id``: ``str | None``.

Output: ``{"workflow_result": <WorkflowResult dict>}`` — the key matches the conventional
output data unit name ``workflow_result``.
"""

from __future__ import annotations

import logging
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig

from apecx_integration.composition.handles.store import HandleStore, default_handle_store
from apecx_integration.composition.schemas.data_shapes import parse_data_shape
from apecx_integration.composition.schemas.workflow_result import WorkflowResult

log = logging.getLogger(__name__)

_OUTPUT_KEY = "workflow_result"


class EnvelopeStepConfig(StepConfig):
    """EnvelopeStep config — which input keys carry the markdown / data payload.

    Defaults match a step authored to receive ``{"markdown": ..., "data": ...}`` directly.
    Appending EnvelopeStep after a step that emits a different key (e.g. ``rag_synthesis``
    emits ``{"synthesis": "<md>"}``) only needs ``markdown_input_key: synthesis`` — no
    TransformLink, no shared-class edit (EO-13c).
    """

    markdown_input_key: str = "markdown"
    data_input_key: str = "data"


class EnvelopeStep(BaseStep):
    @classmethod
    def _get_config_class(cls):
        return EnvelopeStepConfig

    def _init_from_config(self, config, component_config, dependencies) -> None:
        super()._init_from_config(config, component_config, dependencies)
        # Read the configurable keys off the validated config object.
        self._markdown_key: str = getattr(config, "markdown_input_key", "markdown")
        self._data_key: str = getattr(config, "data_input_key", "data")

    def _handle_store(self) -> HandleStore:
        # Indirection so a test or a deployment can swap the store.
        return default_handle_store()

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"EnvelopeStep '{self.name}': input_data must be a dict, got "
                f"{type(input_data).__name__}"
            )
        markdown_key = getattr(self, "_markdown_key", "markdown")
        data_key = getattr(self, "_data_key", "data")

        # Unwrap the framework trigger envelope ({du_name: payload}) generically: when the
        # markdown key is not already at top level and this is a single-key wrapper whose
        # value is itself a dict, descend into that dict. Covers both a step authored to
        # take {markdown: ...} directly AND a link that delivers an upstream step's whole
        # output dict (e.g. {"synthesis": "<md>"}) into this step's single input DU.
        if markdown_key not in input_data and len(input_data) == 1:
            only_value = next(iter(input_data.values()))
            if isinstance(only_value, dict):
                input_data = only_value

        markdown = input_data.get(markdown_key)
        if not isinstance(markdown, str) or not markdown.strip():
            raise ValueError(
                f"EnvelopeStep '{self.name}': input must carry a non-empty "
                f"{markdown_key!r} string; got {type(markdown).__name__}={markdown!r}"
            )

        data_handle: str | None = None
        data_preview: dict[str, Any] | None = None
        raw_data = input_data.get(data_key)
        if raw_data is not None:
            if not isinstance(raw_data, dict):
                raise ValueError(
                    f"EnvelopeStep '{self.name}': {data_key!r} must be a serialized DataShape "
                    f"dict, got {type(raw_data).__name__}"
                )
            shape = parse_data_shape(raw_data)  # loud on bad/missing kind
            data_handle = self._handle_store().put(shape)
            data_preview = shape.preview()

        result = WorkflowResult(
            markdown=markdown,
            data_handle=data_handle,
            data_preview=data_preview,
            run_id=input_data.get("run_id"),
        )
        log.info(
            "EnvelopeStep %s: emitted WorkflowResult (md=%d chars, handle=%s)",
            self.name,
            len(markdown),
            data_handle,
        )
        return {_OUTPUT_KEY: result.model_dump(mode="json")}
