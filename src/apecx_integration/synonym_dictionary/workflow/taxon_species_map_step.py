"""TaxonSpeciesMapStep — build the strain→species map into the dictionary.

Strain→species normalization (2026-06-09). A record stamped with a
strain-level NCBI taxon (e.g. BVBRC genome-id prefix 1001772, a specific
Influenza A strain) does not match a query for the SPECIES IRI (11320,
Influenza A virus). This step precomputes, for every taxon at-or-below
species rank, its species-rank ancestor, and writes a ``taxon_species``
table into the dictionary. A consumer (the harvester republish resolver,
via ``dict_reader``) then stamps BOTH the record's taxon AND its species,
so ``subjects.valueUri`` queries for the species match strain-level
records uniformly across sources.

The heavy graph traversal happens here, once, at build/enrichment time;
the runtime is a single indexed table read.

Framework compliance (see the nanobrain-step-authoring skill):
- ``from_config``-only construction; subclass of ``BaseStep``.
- Implements ``process()``; never overrides ``execute()``.
- Owns its input/output data units + trigger (declared in the YAML); the
  workflow owns the links.
- Logs via ``self.nb_logger``.

Input contract (under data unit ``species_map_input``)::

    {"nodes_path": "/abs/path/to/nodes.dmp",
     "dictionary_path": "/abs/path/to/dictionary.sqlite"}

Output (under ``species_map_result``)::

    {"dictionary_path": "/abs/.../dictionary.sqlite",
     "taxa_mapped": <int>, "species_count": <int>}
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field

_INPUT_DU = "species_map_input"
_OUTPUT_KEY = "species_map_result"


class TaxonSpeciesMapStepConfig(StepConfig):
    """Config for :class:`TaxonSpeciesMapStep`.

    ``species_rank`` is the NCBI rank string treated as the normalization
    target (default ``"species"``). Exposed for the rare caller that wants
    to normalise to a different rank (e.g. ``"genus"``).

    ``extra='forbid'`` per workspace policy; ``validate_assignment=False``
    so the framework can ``setattr`` ``source_path`` after validation (the
    same shape as DictionaryBuildStepConfig).
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
        use_enum_values=False,
        validate_assignment=False,
        str_strip_whitespace=False,
    )

    # Framework-set after validation by ConfigBase.from_config(); declared
    # explicitly so the post-validation setattr doesn't trip extra='forbid'.
    source_path: str | None = Field(
        default=None,
        description="Framework-set path of the YAML the config was loaded from.",
    )

    species_rank: str = Field(
        default="species",
        description="NCBI rank string to normalise strain/subspecies taxa up to.",
    )


class TaxonSpeciesMapStep(BaseStep):
    """Compute + persist the strain→species map into the dictionary SQLite."""

    COMPONENT_TYPE: str = "taxon_species_map_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return TaxonSpeciesMapStepConfig

    @classmethod
    def extract_component_config(cls, config: TaxonSpeciesMapStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {**base, "species_rank": config.species_rank}

    def _init_from_config(
        self,
        config: TaxonSpeciesMapStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._species_rank: str = component_config.get("species_rank", "species")

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        payload = self._unwrap(input_data)
        nodes_path = Path(payload["nodes_path"])
        dict_path = Path(payload["dictionary_path"])

        self.nb_logger.info(
            "TaxonSpeciesMapStep %s: computing species ancestors from %s",
            self.name,
            nodes_path,
        )

        # Heavy graph traversal off the event loop (CPU-bound over ~2.8M taxa).
        import asyncio  # noqa: PLC0415

        species_map = await asyncio.to_thread(self._compute, nodes_path)
        species_count = len(set(species_map.values()))
        self.nb_logger.info(
            "TaxonSpeciesMapStep %s: %d taxa → %d distinct species; writing taxon_species",
            self.name,
            len(species_map),
            species_count,
        )

        written = await asyncio.to_thread(self._write, dict_path, species_map)

        return {
            _OUTPUT_KEY: {
                "dictionary_path": str(dict_path),
                "taxa_mapped": written,
                "species_count": species_count,
            }
        }

    def _compute(self, nodes_path: Path) -> dict[int, int]:
        from apecx_integration.synonym_dictionary.hierarchy_loader import (  # noqa: PLC0415
            compute_species_ancestors,
        )

        return compute_species_ancestors(nodes_path, species_rank=self._species_rank)

    def _write(self, dict_path: Path, species_map: dict[int, int]) -> int:
        from apecx_integration.synonym_dictionary.sqlite_writer import (  # noqa: PLC0415
            SQLiteDictionaryWriter,
        )

        writer = SQLiteDictionaryWriter(dict_path)
        try:
            con = writer._conn  # noqa: SLF001 — bulk-transaction control
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            written = writer.write_taxon_species(iter(species_map.items()))
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return written
        finally:
            writer.close()

    def _unwrap(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Unwrap the framework trigger envelope ``{du_name: payload}``."""
        if not isinstance(input_data, dict):
            raise ValueError(
                f"TaxonSpeciesMapStep '{self.name}': input_data must be a dict, "
                f"got {type(input_data).__name__}"
            )
        if (
            _INPUT_DU in input_data
            and isinstance(input_data[_INPUT_DU], dict)
            and "nodes_path" not in input_data
        ):
            input_data = input_data[_INPUT_DU]
        for key in ("nodes_path", "dictionary_path"):
            if key not in input_data:
                raise ValueError(
                    f"TaxonSpeciesMapStep '{self.name}': input missing required "
                    f"key {key!r}; got keys {sorted(input_data)}"
                )
        return input_data

    @staticmethod
    def species_for_taxon(con: sqlite3.Connection, taxon_id: int) -> int | None:
        """Read helper: the species ancestor of ``taxon_id`` (or None)."""
        row = con.execute(
            "SELECT species_taxon_id FROM taxon_species WHERE taxon_id = ?",
            (taxon_id,),
        ).fetchone()
        return int(row[0]) if row else None
