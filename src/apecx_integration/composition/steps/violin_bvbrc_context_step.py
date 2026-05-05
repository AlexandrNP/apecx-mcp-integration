"""Nanobrain ``BaseStep`` wrapper around a pure-pandas substring
lookup over the VIOLIN + BV-BRC tabular fixtures.

This step contributes two of the four retrieval branches feeding
the synthesis pipeline:

- ``violin_mappings``: rows of ``Pathogen_Information.csv`` and
  ``Vaccine_Information.csv`` whose Pathogen / Vaccine name (case-
  insensitive) contains any of the input entity names.
- ``bvbrc_genomes``: rows of ``alphavirus_genomes.tsv`` whose
  ``genome.genome_name`` matches any input entity name.

NO LLM. The match is substring-only — no embedding, no fuzzy
distance — so this step is fast, deterministic, and cheap to run
on every query.

Operator-level hooks
--------------------
- ``violin_data_dir`` / ``bvbrc_cache_dir``: path overrides.
  Resolution order:
    1. value from this YAML (if set, non-null)
    2. ``APECX_DB_DATA_DIR`` env var (VIOLIN only)
    3. workspace-relative ``data/violin/`` and ``data/bvbrc_cache/``
- ``max_violin_mappings`` / ``max_bvbrc_genomes``: caps applied
  after substring matching. Wide queries (e.g. just "virus") can
  match tens of thousands of rows; the cap protects the downstream
  LLM context budget.

Failure mode
------------
Missing CSV / TSV files: log a WARNING and return an empty list
for that source. Partial-source results still feed synthesis (the
synthesizer's empty-retrieval gate fires only if EVERY source is
empty).

Authoring rule alignment (nanobrain-step-authoring skill)
---------------------------------------------------------
- Implements ``async def process()`` — never overrides ``execute()``.
- ``COMPONENT_TYPE`` + ``REQUIRED_CONFIG_FIELDS`` declared.
- Loaded via ``from_config(YAML)`` only — direct constructor is
  forbidden by the framework.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

from apecx_integration._workspace import resolve_workspace_root

log = logging.getLogger(__name__)


_WORKSPACE_ROOT = resolve_workspace_root(__file__, fallback_depth=5)
_DEFAULT_VIOLIN_DIR = _WORKSPACE_ROOT / "data" / "violin"
_DEFAULT_BVBRC_DIR = _WORKSPACE_ROOT / "data" / "bvbrc_cache"


class VIOLINBVBRCContextStepConfig(StepConfig):
    """Step config for VIOLINBVBRCContextStep.

    ``extra='forbid'`` (workspace rule): YAML typos raise rather
    than silently using defaults.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    # Framework tracking attribute set by ConfigBase.from_config after
    # construction. Declared here so extra="forbid" doesn't block setattr.
    source_path: str | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        # nanobrain ConfigBase passes the raw YAML dict to Pydantic;
        # the top-level ``class`` key is a framework identifier, not a
        # config field. Strip it before extra="forbid" fires.
        if isinstance(data, dict):
            data.pop("class", None)
        return data

    violin_data_dir: str | None = Field(
        default=None,
        description=(
            "Path to the directory containing VIOLIN CSVs "
            "(``Pathogen_Information.csv`` + "
            "``Vaccine_Information.csv``). When None, falls back to "
            "the ``APECX_DB_DATA_DIR`` env var, then to the "
            "workspace-relative ``data/violin/``."
        ),
    )
    bvbrc_cache_dir: str | None = Field(
        default=None,
        description=(
            "Path to the directory containing the BV-BRC cache "
            "(``alphavirus_genomes.tsv``). When None, falls back to "
            "the workspace-relative ``data/bvbrc_cache/``."
        ),
    )
    max_violin_mappings: int = Field(
        default=10,
        description="Cap on VIOLIN mappings returned per query.",
    )
    max_bvbrc_genomes: int = Field(
        default=10,
        description="Cap on BV-BRC genomes returned per query.",
    )


class VIOLINBVBRCContextStep(BaseStep):
    """Retrieval branch — VIOLIN + BV-BRC tabular lookup (no LLM).

    Expected ``process()`` input::

        {"entities": [
            {"name": "EEEV", "type": "pathogen"},
            {"name": "Sindbis", "type": "pathogen"},
        ]}

    or::

        {"query_terms": ["EEEV", "Sindbis"]}

    At least one of ``entities`` / ``query_terms`` must be present
    AND non-empty; otherwise this step has nothing to match on and
    returns empty bundles for both sources.

    Return shape::

        {"violin_mappings": [
            {"synonym_id": "VIOLIN_pathogen_42",
             "canonical_term": "11036",      # NCBI taxonomy ID
             "query_term": "EEEV",
             "entity_type": "pathogen",
             "source": "VIOLIN_Pathogen_Information"},
            ...
         ],
         "bvbrc_genomes": [
            {"genome_id": "11036.7",
             "genome_name": "Eastern equine encephalitis virus ..."},
            ...
         ]}
    """

    COMPONENT_TYPE: str = "violin_bvbrc_context_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return VIOLINBVBRCContextStepConfig

    @classmethod
    def extract_component_config(cls, config: VIOLINBVBRCContextStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "violin_data_dir": getattr(config, "violin_data_dir", None),
            "bvbrc_cache_dir": getattr(config, "bvbrc_cache_dir", None),
            "max_violin_mappings": getattr(config, "max_violin_mappings", 10),
            "max_bvbrc_genomes": getattr(config, "max_bvbrc_genomes", 10),
        }

    def _init_from_config(
        self,
        config: VIOLINBVBRCContextStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._violin_data_dir: str | None = component_config.get("violin_data_dir")
        self._bvbrc_cache_dir: str | None = component_config.get("bvbrc_cache_dir")
        self._max_violin: int = int(component_config.get("max_violin_mappings", 10))
        self._max_bvbrc: int = int(component_config.get("max_bvbrc_genomes", 10))

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    def _resolve_violin_dir(self) -> Path:
        if self._violin_data_dir:
            return Path(self._violin_data_dir)
        env = os.environ.get("APECX_DB_DATA_DIR")
        if env:
            return Path(env)
        return _DEFAULT_VIOLIN_DIR

    def _resolve_bvbrc_dir(self) -> Path:
        if self._bvbrc_cache_dir:
            return Path(self._bvbrc_cache_dir)
        return _DEFAULT_BVBRC_DIR

    # ------------------------------------------------------------------
    # Input normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_entities(
        input_data: dict[str, Any],
    ) -> list[tuple[str, str]]:
        """Project ``entities`` and ``query_terms`` into a list of
        ``(name, type)`` pairs. ``type`` defaults to ``"unknown"``
        when missing (only used when reporting back as
        ``entity_type`` on a VIOLIN mapping)."""
        out: list[tuple[str, str]] = []
        seen: set[str] = set()

        entities = input_data.get("entities")
        if isinstance(entities, list):
            for ent in entities:
                if isinstance(ent, dict):
                    name = ent.get("name")
                    etype = ent.get("type", "unknown")
                elif isinstance(ent, str):
                    name = ent
                    etype = "unknown"
                else:
                    continue
                if isinstance(name, str) and name.strip():
                    key = name.strip().lower()
                    if key not in seen:
                        seen.add(key)
                        out.append((name.strip(), str(etype) if etype else "unknown"))

        query_terms = input_data.get("query_terms")
        if isinstance(query_terms, list):
            for term in query_terms:
                if isinstance(term, str) and term.strip():
                    key = term.strip().lower()
                    if key not in seen:
                        seen.add(key)
                        out.append((term.strip(), "unknown"))

        return out

    # ------------------------------------------------------------------
    # Pandas lookups (sync, called via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _lookup_violin(
        self,
        terms: list[tuple[str, str]],
        violin_dir: Path,
    ) -> list[dict[str, Any]]:
        """Substring-match against pathogen + vaccine CSVs."""
        if not terms:
            return []

        # Local import — pandas is heavy and not all callers of
        # this module instantiate the step.
        import pandas as pd

        pathogen_csv = violin_dir / "Pathogen_Information.csv"
        vaccine_csv = violin_dir / "Vaccine_Information.csv"

        results: list[dict[str, Any]] = []

        if pathogen_csv.is_file():
            try:
                df = pd.read_csv(pathogen_csv, dtype=str).fillna("")
            except Exception as exc:
                log.warning(
                    "VIOLINBVBRCContextStep %s: failed reading %s: %s",
                    self.name,
                    pathogen_csv,
                    exc,
                )
            else:
                # The CSV columns we need: id, Pathogen, NCBI_Taxonomy_ID.
                if "Pathogen" in df.columns:
                    name_lower = df["Pathogen"].astype(str).str.lower()
                    for query, etype in terms:
                        if len(results) >= self._max_violin:
                            break
                        mask = name_lower.str.contains(query.lower(), regex=False, na=False)
                        for _, row in df[mask].iterrows():
                            if len(results) >= self._max_violin:
                                break
                            canonical = str(row.get("NCBI_Taxonomy_ID", ""))
                            if not canonical:
                                canonical = str(row.get("Pathogen", query))
                            results.append(
                                {
                                    "synonym_id": (f"VIOLIN_pathogen_{row.get('id', '')}"),
                                    "canonical_term": canonical,
                                    "query_term": query,
                                    "entity_type": etype,
                                    "source": "VIOLIN_Pathogen_Information",
                                }
                            )
                else:
                    log.warning(
                        "VIOLINBVBRCContextStep %s: %s missing 'Pathogen' column",
                        self.name,
                        pathogen_csv,
                    )
        else:
            log.warning(
                "VIOLINBVBRCContextStep %s: VIOLIN pathogen CSV not "
                "found at %s — returning no pathogen mappings",
                self.name,
                pathogen_csv,
            )

        if len(results) < self._max_violin and vaccine_csv.is_file():
            try:
                df = pd.read_csv(vaccine_csv, dtype=str).fillna("")
            except Exception as exc:
                log.warning(
                    "VIOLINBVBRCContextStep %s: failed reading %s: %s",
                    self.name,
                    vaccine_csv,
                    exc,
                )
            else:
                # Vaccine CSV uses ``Vaccine_Name`` for the display
                # name and ``Vaccine_Ontology_ID`` for the canonical
                # term. Some rows have only ``Vaccine``; match both.
                name_cols = [c for c in ("Vaccine_Name", "Vaccine") if c in df.columns]
                if name_cols:
                    for query, etype in terms:
                        if len(results) >= self._max_violin:
                            break
                        mask = None
                        for col in name_cols:
                            col_lower = df[col].astype(str).str.lower()
                            sub = col_lower.str.contains(query.lower(), regex=False, na=False)
                            mask = sub if mask is None else (mask | sub)
                        if mask is None:
                            continue
                        for _, row in df[mask].iterrows():
                            if len(results) >= self._max_violin:
                                break
                            vac_canonical = str(row.get("Vaccine_Ontology_ID", ""))
                            if not vac_canonical:
                                vac_canonical = str(
                                    row.get("Vaccine_Name", "") or row.get("Vaccine", query)
                                )
                            results.append(
                                {
                                    "synonym_id": (f"VIOLIN_vaccine_{row.get('id', '')}"),
                                    "canonical_term": vac_canonical,
                                    "query_term": query,
                                    # Vaccine matches override entity_type
                                    # to ``vaccine`` — the upstream entity
                                    # type may say ``pathogen`` but we
                                    # matched on a vaccine row.
                                    "entity_type": "vaccine" if etype == "unknown" else etype,
                                    "source": "VIOLIN_Vaccine_Information",
                                }
                            )
                else:
                    log.warning(
                        "VIOLINBVBRCContextStep %s: %s missing vaccine name columns",
                        self.name,
                        vaccine_csv,
                    )
        elif not vaccine_csv.is_file():
            log.warning(
                "VIOLINBVBRCContextStep %s: VIOLIN vaccine CSV not "
                "found at %s — returning no vaccine mappings",
                self.name,
                vaccine_csv,
            )

        return results

    def _lookup_bvbrc(
        self,
        terms: list[tuple[str, str]],
        bvbrc_dir: Path,
    ) -> list[dict[str, Any]]:
        if not terms:
            return []

        import pandas as pd

        tsv_path = bvbrc_dir / "alphavirus_genomes.tsv"
        if not tsv_path.is_file():
            log.warning(
                "VIOLINBVBRCContextStep %s: BV-BRC TSV not found at %s — returning no genomes",
                self.name,
                tsv_path,
            )
            return []

        try:
            df = pd.read_csv(tsv_path, sep="\t", dtype=str).fillna("")
        except Exception as exc:
            log.warning(
                "VIOLINBVBRCContextStep %s: failed reading %s: %s",
                self.name,
                tsv_path,
                exc,
            )
            return []

        if "genome.genome_name" not in df.columns:
            log.warning(
                "VIOLINBVBRCContextStep %s: %s missing 'genome.genome_name' column",
                self.name,
                tsv_path,
            )
            return []

        name_lower = df["genome.genome_name"].astype(str).str.lower()
        results: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for query, _etype in terms:
            if len(results) >= self._max_bvbrc:
                break
            mask = name_lower.str.contains(query.lower(), regex=False, na=False)
            for _, row in df[mask].iterrows():
                if len(results) >= self._max_bvbrc:
                    break
                gid = str(row.get("genome.genome_id", ""))
                if gid in seen_ids:
                    continue
                seen_ids.add(gid)
                results.append(
                    {
                        "genome_id": gid,
                        "genome_name": str(row.get("genome.genome_name", "")),
                    }
                )
        return results

    # ------------------------------------------------------------------
    # Step entry point
    # ------------------------------------------------------------------

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"VIOLINBVBRCContextStep '{self.name}': input_data "
                f"must be a dict, got {type(input_data).__name__}"
            )

        terms = self._normalize_entities(input_data)
        if not terms:
            log.info(
                "VIOLINBVBRCContextStep %s: no entities/query_terms "
                "in input — returning empty bundles",
                self.name,
            )
            return {"violin_mappings": [], "bvbrc_genomes": []}

        violin_dir = self._resolve_violin_dir()
        bvbrc_dir = self._resolve_bvbrc_dir()

        # Both lookups are blocking pandas reads; offload them.
        # Run them concurrently in worker threads — independent
        # I/O, no shared state.
        violin_mappings, bvbrc_genomes = await asyncio.gather(
            asyncio.to_thread(self._lookup_violin, terms, violin_dir),
            asyncio.to_thread(self._lookup_bvbrc, terms, bvbrc_dir),
        )

        log.info(
            "VIOLINBVBRCContextStep %s: terms=%d -> violin=%d, bvbrc=%d",
            self.name,
            len(terms),
            len(violin_mappings),
            len(bvbrc_genomes),
        )
        return {
            "violin_mappings": violin_mappings,
            "bvbrc_genomes": bvbrc_genomes,
        }
