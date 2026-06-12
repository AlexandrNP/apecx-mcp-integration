"""ConservationScoreStep — per-column conservation over a multiple-sequence alignment (EO-52).

Deterministic, dependency-free conservation scoring. Given an aligned FASTA (every record the
same length, gaps as ``-``), it computes for each alignment column:

- ``identity``: fraction of strains carrying the column's consensus (most-common non-gap)
  residue — the interpretable "N% of strains are identical here" signal. Gaps count against
  identity. This is the metric the conserved-site threshold uses.
- ``shannon``: Shannon entropy of the column (bits), and ``shannon_conservation`` = its
  normalized complement (1 = perfectly conserved). A secondary signal.

Conserved sites are columns with ``identity >= conservation_threshold``; contiguous runs of
them (≥ ``min_region_length``) are reported as conserved regions with their consensus motif.

No external dependencies, no mocks: a malformed alignment (unequal lengths, <2 sequences)
FAILS LOUD rather than producing a meaningless score.
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import Field

log = logging.getLogger(__name__)

_AA_ALPHABET = 21  # 20 amino acids + gap, for Shannon normalization


class ConservationScoreStepConfig(StepConfig):
    """Config for conservation scoring.

    A StepConfig subclass — inherits ``extra='allow'`` (the framework injects metadata like
    ``source_path``); does NOT set ``extra='forbid'``.
    """

    conservation_threshold: float = Field(
        default=0.9,
        ge=0.0,
        le=1.0,
        description="A column is 'conserved' when its consensus-residue identity fraction is "
        "at least this value (default 0.9 = 90% of strains identical at that position).",
    )
    min_region_length: int = Field(
        default=1, ge=1, description="Minimum contiguous conserved columns to report as a region."
    )
    include_per_column: bool = Field(
        default=True, description="Include the full per-column table in the output."
    )


class ConservationScoreStep(BaseStep):
    """Score per-column conservation over an aligned FASTA; report conserved sites + regions."""

    COMPONENT_TYPE = "conservation_score_step"

    @classmethod
    def _get_config_class(cls):
        return ConservationScoreStepConfig

    def _init_from_config(self, config, component_config, dependencies) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._threshold: float = float(getattr(config, "conservation_threshold", 0.9))
        self._min_region: int = int(getattr(config, "min_region_length", 1))
        self._include_per_column: bool = bool(getattr(config, "include_per_column", True))

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        import asyncio

        payload = self._unwrap(input_data)
        alignment_fasta = payload.get("alignment_fasta")
        if not isinstance(alignment_fasta, str) or not alignment_fasta.strip():
            raise ValueError(
                f"ConservationScoreStep '{self.name}': input must carry a non-empty "
                f"'alignment_fasta' string; got {type(alignment_fasta).__name__}"
            )

        seqs = _parse_fasta(alignment_fasta)
        if len(seqs) < 2:
            raise ValueError(
                f"ConservationScoreStep '{self.name}': need ≥2 aligned sequences, got {len(seqs)}."
            )
        lengths = {len(s) for _, s in seqs}
        if len(lengths) != 1:
            raise ValueError(
                f"ConservationScoreStep '{self.name}': sequences are not aligned — unequal "
                f"lengths {sorted(lengths)}. Pass an MSA (every record the same length)."
            )

        result = await asyncio.to_thread(self._score, seqs)
        self.nb_logger.info(
            "ConservationScoreStep %s: %d cols, %d conserved sites, %d regions (threshold %.2f)",
            self.name,
            result["alignment_length"],
            len(result["conserved_sites"]),
            len(result["conserved_regions"]),
            self._threshold,
        )
        return {"conservation_result": result}

    def _unwrap(self, input_data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"ConservationScoreStep '{self.name}': input_data must be a dict, got "
                f"{type(input_data).__name__}"
            )
        if "alignment_fasta" not in input_data and len(input_data) == 1:
            only = next(iter(input_data.values()))
            if isinstance(only, dict):
                return only
        return input_data

    def _score(self, seqs: list[tuple[str, str]]) -> dict[str, Any]:
        n = len(seqs)
        length = len(seqs[0][1])
        denom = math.log2(min(n, _AA_ALPHABET)) or 1.0  # n>=2 → denom>=1

        per_column: list[dict[str, Any]] = []
        conserved_flags: list[bool] = []
        identity_sum = 0.0
        for col in range(length):
            symbols = [s[col] for _, s in seqs]
            gaps = sum(1 for ch in symbols if ch == "-")
            non_gap = [ch for ch in symbols if ch != "-"]
            if non_gap:
                consensus, consensus_count = Counter(non_gap).most_common(1)[0]
                identity = consensus_count / n  # gaps count against identity
            else:
                consensus, identity = "-", 0.0

            counts = Counter(symbols)
            shannon = -sum((c / n) * math.log2(c / n) for c in counts.values())
            shannon_conservation = max(0.0, 1.0 - shannon / denom)

            identity_sum += identity
            is_conserved = identity >= self._threshold
            conserved_flags.append(is_conserved)
            per_column.append(
                {
                    "column": col,
                    "consensus": consensus,
                    "identity": round(identity, 4),
                    "gap_fraction": round(gaps / n, 4),
                    "shannon_bits": round(shannon, 4),
                    "shannon_conservation": round(shannon_conservation, 4),
                    "conserved": is_conserved,
                }
            )

        conserved_sites = [
            {"column": c["column"], "consensus": c["consensus"], "identity": c["identity"]}
            for c in per_column
            if c["conserved"]
        ]
        regions = _contiguous_regions(per_column, conserved_flags, self._min_region)

        out: dict[str, Any] = {
            "n_sequences": n,
            "alignment_length": length,
            "conservation_threshold": self._threshold,
            "mean_identity": round(identity_sum / length, 4) if length else 0.0,
            "n_conserved_columns": len(conserved_sites),
            "conserved_sites": conserved_sites,
            "conserved_regions": regions,
        }
        if self._include_per_column:
            out["per_column"] = per_column
        return out


def _parse_fasta(text: str) -> list[tuple[str, str]]:
    """Parse FASTA into ``[(header, sequence)]``. Sequence whitespace/newlines stripped."""
    records: list[tuple[str, str]] = []
    header: str | None = None
    chunks: list[str] = []
    for line in text.splitlines():
        if line.startswith(">"):
            if header is not None:
                records.append((header, "".join(chunks)))
            header = line[1:].strip()
            chunks = []
        elif header is not None:
            chunks.append(line.strip())
    if header is not None:
        records.append((header, "".join(chunks)))
    return records


def _contiguous_regions(
    per_column: list[dict[str, Any]], flags: list[bool], min_length: int
) -> list[dict[str, Any]]:
    """Group contiguous conserved columns into regions (≥ ``min_length``)."""
    regions: list[dict[str, Any]] = []
    start: int | None = None
    for i, flag in enumerate(flags):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            _append_region(regions, per_column, start, i - 1, min_length)
            start = None
    if start is not None:
        _append_region(regions, per_column, start, len(flags) - 1, min_length)
    return regions


def _append_region(
    regions: list[dict[str, Any]],
    per_column: list[dict[str, Any]],
    start: int,
    end: int,
    min_length: int,
) -> None:
    length = end - start + 1
    if length < min_length:
        return
    motif = "".join(per_column[c]["consensus"] for c in range(start, end + 1))
    mean_identity = sum(per_column[c]["identity"] for c in range(start, end + 1)) / length
    regions.append(
        {
            "start": start,
            "end": end,
            "length": length,
            "consensus": motif,
            "mean_identity": round(mean_identity, 4),
        }
    )


__all__ = ["ConservationScoreStep", "ConservationScoreStepConfig"]
