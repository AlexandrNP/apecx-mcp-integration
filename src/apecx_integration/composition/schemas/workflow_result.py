"""WorkflowResult — the standard envelope a workflow returns through the MCP surface.

Two channels (``external_orchestration_design.md`` §5):

- ``markdown``: human/LLM-facing presentation. The external orchestrating LLM reads
  this to reason and synthesize the final answer.
- ``data_handle`` + ``data_preview``: the structured payload, kept OUT of the external
  LLM's context. Workflows chain by passing the handle to the next workflow; the full
  payload never round-trips through the LLM. ``data_preview`` is a small peek so the LLM
  can decide what to do next without ingesting the whole payload.

This is a plain Pydantic model, not a ``from_config`` framework component — it is data,
not a component. ``extra='forbid'`` (workspace convention) so a typo'd field fails loudly
instead of being silently dropped.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from apecx_integration.composition.schemas.control_transfer import ControlTransfer

WorkflowResultStatus = Literal["ok", "partial", "error", "needs_input"]


class WorkflowResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    markdown: str
    status: WorkflowResultStatus = "ok"
    data_handle: str | None = None
    data_preview: dict[str, Any] | None = None
    run_id: str | None = None
    error: str | None = None
    # RoC-1a — return of control to the frontier LLM (set iff status == "needs_input").
    control_transfer: ControlTransfer | None = None
    # E3-8 — per-run reproducibility record: the determinism-relevant params the science
    # stages used (aligner+version, structural query, PDB/SASA settings, UniProt release,
    # LLM model, run_id). A structured side-channel to the markdown — present when the
    # workflow collects it (viral_epitope_analysis), None otherwise.
    provenance: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _check_consistency(self) -> WorkflowResult:
        # A status="error" with no message is the silent-failure shape this envelope
        # exists to prevent: a caller sees "error" but cannot tell what went wrong.
        if self.status == "error" and not (self.error and self.error.strip()):
            raise ValueError(
                "WorkflowResult.status == 'error' requires a non-empty 'error' message"
            )
        if self.status != "error" and self.error is not None:
            raise ValueError(f"WorkflowResult.error must be None when status is {self.status!r}")
        # needs_input ⟺ a control_transfer: a 'needs_input' with nothing to act on is the same
        # silent-failure shape as an error with no message — the caller cannot tell what is needed.
        if self.status == "needs_input" and self.control_transfer is None:
            raise ValueError("WorkflowResult.status == 'needs_input' requires a 'control_transfer'")
        if self.status != "needs_input" and self.control_transfer is not None:
            raise ValueError(
                f"WorkflowResult.control_transfer must be None when status is {self.status!r}"
            )
        # A preview is a peek at the handle's payload; a preview with no handle would
        # mislead the orchestrating LLM into thinking chainable data exists.
        if self.data_preview is not None and self.data_handle is None:
            raise ValueError("WorkflowResult.data_preview requires data_handle to be set")
        return self

    @classmethod
    def failed(cls, error: str, *, markdown: str = "", run_id: str | None = None) -> WorkflowResult:
        """Construct a loud-error result. Prefer this over hand-setting ``status``."""
        return cls(markdown=markdown, status="error", error=error, run_id=run_id)

    @classmethod
    def needs_input(
        cls,
        control_transfer: ControlTransfer,
        *,
        markdown: str = "",
        run_id: str | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> WorkflowResult:
        """Construct a return-of-control result. Prefer this over hand-setting ``status``."""
        return cls(
            markdown=markdown,
            status="needs_input",
            control_transfer=control_transfer,
            run_id=run_id,
            provenance=provenance,
        )
