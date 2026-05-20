"""Canonical data shapes stored behind a ``WorkflowResult.data_handle`` (EO-12).

Workflows chain by passing a handle to a structured payload. To keep
workflow-to-workflow compatibility typed (rather than "it's some dict"), the payload
behind a handle is one of a small controlled set of shapes, discriminated by ``kind``:

- ``RecordSet`` — homogeneous rows (the common tabular case).
- ``Evidence`` — claim/source/score triples for grounded synthesis.
- ``Bundle`` — a named aggregate of heterogeneous sub-results (the escape hatch).
- ``Artifact`` — a pointer to a binary/file payload (not inlined).

Every shape exposes ``.preview(limit)`` returning a small dict suitable for
``WorkflowResult.data_preview`` — the orchestrating LLM reasons over the preview without
ingesting the full payload. ``extra='forbid'`` so a typo'd field fails loudly.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class _ShapeBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecordSet(_ShapeBase):
    kind: Literal["record_set"] = "record_set"
    records: list[dict[str, Any]] = Field(default_factory=list)
    columns: list[str] | None = None

    def preview(self, limit: int = 3) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "count": len(self.records),
            "columns": self.columns,
            "sample": self.records[:limit],
        }


class EvidenceItem(_ShapeBase):
    claim: str
    source: str
    score: float | None = None


class Evidence(_ShapeBase):
    kind: Literal["evidence"] = "evidence"
    items: list[EvidenceItem] = Field(default_factory=list)

    def preview(self, limit: int = 3) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "count": len(self.items),
            "sample": [i.model_dump() for i in self.items[:limit]],
        }


class Bundle(_ShapeBase):
    kind: Literal["bundle"] = "bundle"
    parts: dict[str, Any] = Field(default_factory=dict)

    def preview(self, limit: int = 10) -> dict[str, Any]:
        return {"kind": self.kind, "parts": sorted(self.parts.keys())[:limit]}


class Artifact(_ShapeBase):
    kind: Literal["artifact"] = "artifact"
    uri: str
    media_type: str
    size_bytes: int | None = None
    filename: str | None = None

    def preview(self, limit: int = 0) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "uri": self.uri,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
        }


DataShape = Annotated[
    RecordSet | Evidence | Bundle | Artifact,
    Field(discriminator="kind"),
]

_DATA_SHAPE_ADAPTER: TypeAdapter[DataShape] = TypeAdapter(DataShape)


def parse_data_shape(payload: dict[str, Any]) -> DataShape:
    """Parse a serialized payload into its typed shape via the ``kind`` discriminator.

    Raises ``pydantic.ValidationError`` on an unknown/missing ``kind`` or a typo'd field —
    a deliberately loud failure rather than a silently-accepted malformed handle payload.
    """
    return _DATA_SHAPE_ADAPTER.validate_python(payload)
