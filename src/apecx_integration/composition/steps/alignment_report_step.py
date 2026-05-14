"""AlignmentReportStep — the "use the result" leg of the MUSCLE workflow.

Final step of the ``rhea_muscle_alignment`` workflow. Consumes the
``RheaFileToolStep`` output, parses the ``out_align`` FASTA alignment,
and computes a small set of alignment statistics plus a human-readable
summary.

Framework-native packaging:
  - Subclasses ``BaseStep``; implements ``async def process``; never
    overrides ``execute()``.
  - Config extends ``StepConfig``; ``extra='forbid'`` (workspace rule).
  - No LLM, no prompts — this is a pure deterministic transformation.

Silent-failure discipline: FAIL-LOUD if ``out_align`` is missing from
the upstream output_files, or if the parsed alignment has zero
sequences. A green upstream call whose alignment we cannot use is
exactly the failure shape worth raising on.
"""

from __future__ import annotations

import logging
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

log = logging.getLogger(__name__)


class AlignmentReportStepConfig(StepConfig):
    """Configuration for :class:`AlignmentReportStep`.

    ``extra='forbid'`` (workspace rule): YAML typos raise at load.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    # Populated by ConfigBase.from_config — declared so extra='forbid'
    # accepts it.
    source_path: str | None = Field(default=None)

    alignment_file_key: str = Field(
        default="out_align",
        description=(
            "Key in the upstream output_files dict that holds the "
            "aligned FASTA. MUSCLE's FASTA alignment output is "
            "'out_align'."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


def _parse_fasta(text: str) -> list[tuple[str, str]]:
    """Parse FASTA text into a list of ``(id, sequence)`` tuples.

    The sequence id is the first whitespace-delimited token of the
    header line (without the leading ``>``). Sequence lines are
    concatenated verbatim — including gap characters — so the caller
    can measure alignment columns.
    """
    records: list[tuple[str, str]] = []
    current_id: str | None = None
    current_seq: list[str] = []
    for line in text.splitlines():
        if line.startswith(">"):
            if current_id is not None:
                records.append((current_id, "".join(current_seq)))
            current_id = line[1:].strip().split()[0] if line[1:].strip() else ""
            current_seq = []
        elif current_id is not None:
            current_seq.append(line.strip())
    if current_id is not None:
        records.append((current_id, "".join(current_seq)))
    return records


class AlignmentReportStep(BaseStep):
    """Parse a MUSCLE alignment and produce a summary + statistics.

    Expected ``process()`` input — the ``RheaFileToolStep`` output::

        {
            "tool_name": "muscle",
            "return_code": 0,
            "output_files": {"out_align": "<aligned FASTA>", ...},
            ...
        }

    Return shape::

        {
            "summary": "<human-readable text>",
            "n_sequences": 5,
            "alignment_length": 412,
            "alignment_fasta": "<the out_align FASTA verbatim>",
            "per_sequence": [{"id": "TestSequence1", "gap_fraction": 0.02}, ...],
        }
    """

    COMPONENT_TYPE: str = "alignment_report_step"
    REQUIRED_CONFIG_FIELDS = ["name"]

    @classmethod
    def _get_config_class(cls):
        return AlignmentReportStepConfig

    def _init_from_config(
        self,
        config: AlignmentReportStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._alignment_file_key = config.alignment_file_key

    async def process(self, input_data: Any, **kwargs) -> dict[str, Any]:
        """Parse the alignment FASTA and compute the report."""
        payload = input_data if isinstance(input_data, dict) else {}
        # Unwrap a single-key trigger envelope whose value is the real
        # RheaFileToolStep output dict.
        if len(payload) == 1 and not (
            {"output_files", "tool_name", "return_code"} & payload.keys()
        ):
            (only_value,) = payload.values()
            if isinstance(only_value, dict):
                payload = only_value

        output_files = payload.get("output_files")
        if not isinstance(output_files, dict):
            raise ValueError(
                f"AlignmentReportStep {self.name!r}: input is missing the "
                f"'output_files' dict from the upstream RheaFileToolStep "
                f"(got keys {sorted(payload)})"
            )

        key = self._alignment_file_key
        alignment_fasta = output_files.get(key)
        if not isinstance(alignment_fasta, str) or not alignment_fasta.strip():
            raise ValueError(
                f"AlignmentReportStep {self.name!r}: upstream output_files "
                f"has no usable {key!r} alignment FASTA "
                f"(available keys: {sorted(output_files)})"
            )

        records = _parse_fasta(alignment_fasta)
        if not records:
            raise ValueError(
                f"AlignmentReportStep {self.name!r}: the {key!r} alignment "
                f"FASTA parsed to zero sequences — it is not a usable "
                f"alignment"
            )

        n_sequences = len(records)
        alignment_length = max(len(seq) for _, seq in records)

        per_sequence: list[dict[str, Any]] = []
        for seq_id, seq in records:
            gap_count = seq.count("-")
            gap_fraction = round(gap_count / len(seq), 4) if seq else 0.0
            per_sequence.append({"id": seq_id, "gap_fraction": gap_fraction})

        tool_name = payload.get("tool_name", "unknown")
        mean_gap = (
            round(
                sum(p["gap_fraction"] for p in per_sequence) / n_sequences,
                4,
            )
            if n_sequences
            else 0.0
        )
        summary_lines = [
            f"{tool_name} alignment report",
            f"  sequences aligned : {n_sequences}",
            f"  alignment length  : {alignment_length} columns",
            f"  mean gap fraction : {mean_gap}",
            "  per-sequence gap fraction:",
        ]
        for p in per_sequence:
            summary_lines.append(f"    {p['id']}: {p['gap_fraction']}")
        summary = "\n".join(summary_lines)

        self.nb_logger.info(
            "AlignmentReportStep %r: %d sequences, %d columns, mean gap %s",
            self.name,
            n_sequences,
            alignment_length,
            mean_gap,
        )
        return {
            "summary": summary,
            "n_sequences": n_sequences,
            "alignment_length": alignment_length,
            "alignment_fasta": alignment_fasta,
            "per_sequence": per_sequence,
        }


__all__ = ["AlignmentReportStep", "AlignmentReportStepConfig"]
