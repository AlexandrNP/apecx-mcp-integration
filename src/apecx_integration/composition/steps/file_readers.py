"""Generic delimited-file reader (T02 Phase 3).

Covers both the VIOLIN CSV inputs (``data/violin/*.csv``) and the
BV-BRC snapshot TSVs (``data/bvbrc_cache/*.tsv``) via a single
configurable step. The only difference between the two cases is the
delimiter and optional schema-required-columns list; everything else
(path resolution, encoding auto-detect, output shape) is shared.

## Why author this

The T02 Phase 3 audit (2026-04-22) scanned
``nanobrain/library/`` and confirmed no generic Step-level CSV / TSV
reader exists. What does exist (``library/tools/search/csv_processor.py``)
is a sync Tool, not a ``from_config``-compatible Step, and can't be
wrapped directly. The ``_load_csv_data`` method on
``enhanced_bv_brc_data_acquisition_step`` is an empty placeholder.

## Output shape

``{"records": [dict, dict, ...], "row_count": int, "source_path": str}``.

Records are ``list[dict[str, str]]`` — values are strings as pandas
would return from ``dtype=str``. Downstream steps coerce types as
needed. Simpler than deciding "what dtype should NCBI_Taxonomy_ID
be?" at the reader layer.

## Schema validation

Optional ``required_columns: list[str]``. If any listed column is
missing from the header, step init fails immediately rather than
silently producing rows with ``KeyError`` surprises at downstream
steps. Other columns are allowed (VIOLIN CSVs have 24 columns; we
don't want to re-spec them all).

## Design choices

- **No pandas dep at step level.** ``csv.DictReader`` covers the use
  case with zero external dependencies. Pandas is already a nanobrain
  dep but pulling it in per-step bloats import time.
- **Path is config-time, not runtime.** In this workflow the file
  path is fixed per step (``VIOLIN_vaccine_reader`` always reads
  ``Vaccine_Information.csv``). A caller that needs runtime
  path selection should instantiate a different step per path.
- **No streaming.** VIOLIN CSVs are ≤10k rows and BV-BRC snapshots are
  ≤10k genomes. Loading into memory is acceptable; streaming would be
  premature at this scale.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any, Literal

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import Field

log = logging.getLogger(__name__)

# G34 (closed 2026-05-09): nanobrain's ConfigBase now sets
# ``str_strip_whitespace=False`` (config_base.py:684), so passing
# ``delimiter: "\t"`` in YAML arrives intact. The named-format enum
# below is preserved for backward compatibility with existing YAMLs
# that already use ``format: csv|tsv``, but new authors can also
# pass ``delimiter`` directly with arbitrary whitespace-significant
# characters. Regression-pinned at
# ``nanobrain/tests/unit/test_g34_strip_whitespace_off.py``.
_FORMAT_TO_DELIMITER: dict[str, str] = {
    "csv": ",",
    "tsv": "\t",
}


class DelimitedFileReaderStepConfig(StepConfig):
    file_path: str
    format: Literal["csv", "tsv"] = "csv"
    # G34 — explicit ``delimiter`` overrides ``format`` when both are
    # set. None preserves backward compat (format-derived).
    delimiter: str | None = Field(
        default=None,
        description=(
            "Optional explicit delimiter. When set, overrides ``format``. "
            "Whitespace-significant chars (\\t, multi-char separators) are "
            "preserved as of G34 (str_strip_whitespace=False). When None, "
            "the delimiter is derived from ``format``."
        ),
    )
    encoding: str = "utf-8"
    required_columns: list[str] = Field(default_factory=list)


class DelimitedFileReaderStep(BaseStep):
    """Load a CSV / TSV file into a list of dicts.

    Config required fields: ``file_path`` (absolute or relative to
    the current working directory). Config optional fields:
    ``delimiter`` (default ","), ``encoding`` (default "utf-8"),
    ``required_columns`` (default []).
    """

    COMPONENT_TYPE: str = "delimited_file_reader_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name", "file_path"]

    @classmethod
    def _get_config_class(cls):
        return DelimitedFileReaderStepConfig

    @classmethod
    def extract_component_config(cls, config: DelimitedFileReaderStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "file_path": config.file_path,
            "format": getattr(config, "format", "csv"),
            "delimiter": getattr(config, "delimiter", None),
            "encoding": getattr(config, "encoding", "utf-8"),
            "required_columns": list(getattr(config, "required_columns", []) or []),
        }

    def _init_from_config(
        self,
        config: DelimitedFileReaderStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._file_path: str = component_config["file_path"]
        format_name: str = component_config.get("format", "csv") or "csv"
        if format_name not in _FORMAT_TO_DELIMITER:
            raise ValueError(
                f"DelimitedFileReaderStep '{self.name}': format must be one of "
                f"{sorted(_FORMAT_TO_DELIMITER)}, got {format_name!r}."
            )
        self._format: str = format_name
        # G34 — explicit delimiter overrides format-derived. Whitespace-
        # significant chars are now preserved end-to-end.
        explicit_delimiter = component_config.get("delimiter")
        self._delimiter: str = (
            explicit_delimiter
            if explicit_delimiter is not None
            else _FORMAT_TO_DELIMITER[format_name]
        )
        self._encoding: str = component_config.get("encoding") or "utf-8"
        self._required_columns: list[str] = component_config["required_columns"]

    async def process(self, input_data: dict[str, Any] | None = None, **kwargs) -> dict[str, Any]:
        path = Path(self._file_path)
        if not path.is_file():
            raise FileNotFoundError(
                f"DelimitedFileReaderStep '{self.name}': file not found at "
                f"{path.resolve()}. Check the config's file_path and the "
                f"working directory at step-run time."
            )

        records: list[dict[str, str]] = []
        with path.open(newline="", encoding=self._encoding) as f:
            reader = csv.DictReader(f, delimiter=self._delimiter)
            fieldnames = reader.fieldnames or []
            missing = [c for c in self._required_columns if c not in fieldnames]
            if missing:
                raise ValueError(
                    f"DelimitedFileReaderStep '{self.name}': file "
                    f"{path.resolve()} is missing required columns {missing!r}. "
                    f"Actual columns: {fieldnames!r}."
                )
            for row in reader:
                records.append(dict(row))

        log.info(
            "DelimitedFileReaderStep %s: read %d rows from %s (format=%s)",
            self.name,
            len(records),
            path.name,
            self._format,
        )
        return {
            "records": records,
            "row_count": len(records),
            "source_path": str(path.resolve()),
        }
