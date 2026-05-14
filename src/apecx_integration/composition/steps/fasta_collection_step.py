"""FastaCollectionStep — the "collect data" leg of the MUSCLE workflow.

First step of the ``rhea_muscle_alignment`` workflow. Resolves the
input FASTA from one of three sources, in priority order:

  1. ``input_data['fasta_path']`` — an explicit path supplied per-call.
  2. ``input_data['fasta_text']`` — raw FASTA text supplied per-call.
  3. ``default_fasta_path`` from the step config — the bundled example
     (``data/seqtest.fasta`` next to the workflow YAML).

It emits the staged-file payload the downstream ``RheaFileToolStep``
expects: ``{fasta_name, fasta_bytes, n_sequences}``.

Framework-native packaging:
  - Subclasses ``BaseStep``; implements ``async def process``; never
    overrides ``execute()``.
  - Config extends ``StepConfig``; ``extra='forbid'`` (workspace rule).
  - ``default_fasta_path`` resolution: an absolute path is used as-is;
    a relative path is resolved against the step YAML's directory
    (``source_path``, populated by ConfigBase.from_config) so the step
    works whether the workflow is run from the repo root or elsewhere.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

log = logging.getLogger(__name__)


class FastaCollectionStepConfig(StepConfig):
    """Configuration for :class:`FastaCollectionStep`.

    ``extra='forbid'`` (workspace rule): YAML typos raise at load.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    # Populated by ConfigBase.from_config — declared so extra='forbid'
    # accepts it, and used to resolve a relative default_fasta_path.
    source_path: str | None = Field(default=None)

    default_fasta_path: str = Field(
        ...,
        description=(
            "Path to the bundled example FASTA. Relative paths are "
            "resolved against this YAML's directory; absolute paths "
            "are used as-is. Used only when neither fasta_path nor "
            "fasta_text is supplied to process()."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


class FastaCollectionStep(BaseStep):
    """Resolve the input FASTA and emit the staged-file payload.

    Expected ``process()`` input (all keys optional)::

        {"fasta_path": "/abs/or/rel/path.fasta"}
        # or
        {"fasta_text": ">seq1\\nACGT...\\n"}
        # or
        {}   # falls back to default_fasta_path

    Return shape::

        {
            "fasta_name": "seqtest.fasta",
            "fasta_bytes": b">seq1\\n...",
            "n_sequences": 5,
        }
    """

    COMPONENT_TYPE: str = "fasta_collection_step"
    REQUIRED_CONFIG_FIELDS = ["name", "default_fasta_path"]

    @classmethod
    def _get_config_class(cls):
        return FastaCollectionStepConfig

    def _init_from_config(
        self,
        config: FastaCollectionStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._default_fasta_path = self._resolve_default_path(config)

    @staticmethod
    def _resolve_default_path(config: FastaCollectionStepConfig) -> Path:
        """Resolve ``default_fasta_path`` to an absolute Path.

        Absolute paths are used as-is. Relative paths resolve against
        the step YAML's directory (``config.source_path``) so the
        workflow runs correctly from any cwd. Existence is NOT checked
        here — a missing file should fail at process() time with a
        clear message, not block step construction (the per-call
        fasta_path/fasta_text inputs may make the default moot).
        """
        raw = Path(config.default_fasta_path)
        if raw.is_absolute():
            return raw
        if config.source_path:
            return (Path(config.source_path).parent / raw).resolve()
        return raw.resolve()

    @staticmethod
    def _count_sequences(fasta_bytes: bytes) -> int:
        """Count FASTA records by counting ``>`` header lines."""
        return sum(1 for line in fasta_bytes.splitlines() if line.startswith(b">"))

    async def process(self, input_data: Any, **kwargs) -> dict[str, Any]:
        """Resolve the FASTA from the input or the bundled default."""
        payload = input_data if isinstance(input_data, dict) else {}
        # The trigger system may wrap the payload as
        # {<input_du_name>: <payload>}; unwrap a single-key envelope
        # whose value is itself a dict and whose key is not one of our
        # own payload keys.
        if len(payload) == 1 and not ({"fasta_path", "fasta_text"} & payload.keys()):
            (only_value,) = payload.values()
            if isinstance(only_value, dict):
                payload = only_value

        fasta_path = payload.get("fasta_path")
        fasta_text = payload.get("fasta_text")

        if isinstance(fasta_text, str) and fasta_text.strip():
            fasta_bytes = fasta_text.encode("utf-8")
            fasta_name = "input.fasta"
        elif isinstance(fasta_path, str) and fasta_path.strip():
            path = Path(fasta_path)
            if not path.is_absolute() and self._default_fasta_path:
                # A relative per-call path is resolved against cwd —
                # that is the caller's responsibility; resolve() makes
                # the eventual error message absolute.
                path = path.resolve()
            if not path.is_file():
                raise ValueError(
                    f"FastaCollectionStep {self.name!r}: fasta_path {path} does not exist"
                )
            fasta_bytes = path.read_bytes()
            fasta_name = path.name
        else:
            path = self._default_fasta_path
            if not path.is_file():
                raise ValueError(
                    f"FastaCollectionStep {self.name!r}: no fasta_path / "
                    f"fasta_text supplied and the bundled default_fasta_path "
                    f"{path} does not exist"
                )
            fasta_bytes = path.read_bytes()
            fasta_name = path.name

        n_sequences = self._count_sequences(fasta_bytes)
        if n_sequences == 0:
            raise ValueError(
                f"FastaCollectionStep {self.name!r}: resolved FASTA "
                f"{fasta_name!r} contains zero '>' header lines — it is "
                f"not a usable FASTA file"
            )

        self.nb_logger.info(
            "FastaCollectionStep %r: collected %r (%d bytes, %d sequences)",
            self.name,
            fasta_name,
            len(fasta_bytes),
            n_sequences,
        )
        return {
            "fasta_name": fasta_name,
            "fasta_bytes": fasta_bytes,
            "n_sequences": n_sequences,
        }


__all__ = ["FastaCollectionStep", "FastaCollectionStepConfig"]
