"""DictionaryBuildStep — nanobrain wrapper around ``build_dictionary``.

Wraps the existing procedural :func:`apecx_integration.synonym_dictionary.build.build_dictionary`
helper as a real nanobrain step. The step takes the ``taxdump_paths``
output from :class:`TaxdumpFetchStep` (a dict of two file-paths) plus
its own config (data table paths, ontology version pins, output dir,
optional row cap) and produces a SQLite dictionary artifact + a
human-inspectable ``manifest.json`` alongside per-table enriched CSVs.

Contract
--------
- Input: a single data unit ``taxdump_paths`` whose value is the dict
  ``{"nodes_path": <str>, "merged_path": <str>}`` emitted by
  :class:`TaxdumpFetchStep`.
- Output: a single data unit ``build_result`` whose value is the dict
  ``{"sqlite_path": <str>, "manifest": <dict>}``.

Why ``manifest`` is a plain dict (not a :class:`BuildManifest`)
---------------------------------------------------------------
DataUnit values must serialise through any future remote transport,
so the step normalises to ``manifest.model_dump(mode='json')`` before
returning. Callers that want the typed object can re-validate via
``BuildManifest.model_validate(...)``.

Framework compliance
--------------------
- Subclasses :class:`BaseStep`, implements ``process()`` only — never
  overrides ``execute``.
- The step owns its input/output data units and trigger; the workflow
  YAML owns the link from the upstream taxdump-fetch step.
- The step's config sets ``extra='forbid'`` per workspace policy.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field

from apecx_integration.synonym_dictionary.build import (
    TableSpec,
    build_dictionary,
)
from apecx_integration.synonym_dictionary.enums import OntologyName
from apecx_integration.synonym_dictionary.resolvers import (
    GeneResolver,
    PathogenResolver,
    VaccineResolver,
)
from apecx_integration.synonym_dictionary.sqlite_writer import (
    SQLiteDictionaryWriter,
)

log = logging.getLogger(__name__)


def _default_dict_output_dir() -> str:
    """Resolve the default dictionary output directory.

    Order of precedence:

    1. ``APECX_DICT_OUTPUT_DIR`` environment variable.
    2. ``~/.apecx/dictionary`` expanded against the caller's home dir.
    """
    env_value = os.environ.get("APECX_DICT_OUTPUT_DIR")
    if env_value:
        return env_value
    return str(Path("~/.apecx/dictionary").expanduser())


def _default_dictionary_version() -> str:
    """Default dictionary_version is the current ISO-8601 UTC timestamp.

    Matches the behaviour of the legacy CLI's
    ``args.dictionary_version or datetime.now(UTC).strftime(...)`` so a
    bootstrap that omits the field gets the same shape as before.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")


def _default_data_file_path(relative_path: str) -> str | None:
    """Resolve data file path from APECX_DATA_ROOT environment variable."""
    data_root = os.environ.get("APECX_DATA_ROOT")
    if data_root:
        return str(Path(data_root) / relative_path)
    return None


def _default_violin_pathogens_path() -> str | None:
    return _default_data_file_path("violin/Pathogen_Information.csv")


def _default_violin_vaccines_path() -> str | None:
    return _default_data_file_path("violin/Vaccine_Information.csv")


def _default_violin_genes_path() -> str | None:
    return _default_data_file_path("violin/Gene_Information.csv")


def _default_bvbrc_genomes_path() -> str | None:
    return _default_data_file_path("BVBRC_genome_alphavirus.csv")


class DictionaryBuildStepConfig(StepConfig):
    """Config for :class:`DictionaryBuildStep`.

    All four input-table paths are optional — table specs whose path is
    ``None`` are simply omitted from the build, matching the legacy CLI.

    ``extra='forbid'`` is set per workspace policy.

    ``validate_assignment=False`` (vs. ConfigBase's ``True`` default)
    so the framework can attach its own post-instantiation attributes
    — notably ``source_path`` — via ``setattr``. Pydantic enforces
    ``extra='forbid'`` at *initial* validation regardless, so the
    workspace policy still catches YAML typos at load time.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
        use_enum_values=False,
        validate_assignment=False,
        str_strip_whitespace=False,
    )

    # Framework-set: ConfigBase.from_config() does
    # ``setattr(config_instance, 'source_path', str(config_path))`` after
    # validation. With ``extra='forbid'`` we must declare it explicitly
    # so the assignment doesn't raise. Optional + default=None so callers
    # constructing the model in-memory don't have to set it.
    source_path: str | None = Field(
        default=None,
        description="Framework-set path of the YAML the config was loaded from.",
    )

    violin_pathogens_path: str | None = Field(
        default_factory=_default_violin_pathogens_path,
        description="Path to VIOLIN Pathogen_Information.csv. Defaults from APECX_DATA_ROOT/violin/Pathogen_Information.csv.",
    )
    violin_vaccines_path: str | None = Field(
        default_factory=_default_violin_vaccines_path,
        description="Path to VIOLIN Vaccine_Information.csv. Defaults from APECX_DATA_ROOT/violin/Vaccine_Information.csv.",
    )
    violin_genes_path: str | None = Field(
        default_factory=_default_violin_genes_path,
        description="Path to VIOLIN Gene_Information.csv. Defaults from APECX_DATA_ROOT/violin/Gene_Information.csv.",
    )
    bvbrc_genomes_path: str | None = Field(
        default_factory=_default_bvbrc_genomes_path,
        description="Path to a BV-BRC genomes TSV. Defaults from APECX_DATA_ROOT/BVBRC_genome_alphavirus.csv.",
    )
    output_dir: str = Field(
        default_factory=_default_dict_output_dir,
        description=(
            "Directory for dictionary.sqlite, manifest.json and the "
            "enriched/ subdirectory. Defaults to "
            "${APECX_DICT_OUTPUT_DIR:-~/.apecx/dictionary}."
        ),
    )
    dictionary_version: str = Field(
        default_factory=_default_dictionary_version,
        description=(
            "Build identifier; defaults to the current ISO-8601 UTC "
            "timestamp at config-validation time."
        ),
    )
    ncbitaxon_version: str = Field(
        default="unknown",
        description="Pinned NCBITaxon release identifier (recorded in manifest).",
    )
    vo_version: str = Field(
        default="unknown",
        description="Pinned Vaccine Ontology release identifier.",
    )
    doid_version: str = Field(
        default="unknown",
        description="Pinned Disease Ontology release identifier.",
    )
    ncbigene_version: str = Field(
        default="unknown",
        description="NCBI Gene build identifier.",
    )
    max_rows: int | None = Field(
        default=None,
        description=(
            "Optional cap on rows-per-table. Useful for smoke-testing "
            "against live OLS without doing a full build. "
            "When None, all rows are processed."
        ),
    )
    execution_timeout: int = Field(
        default=300,
        description=(
            "Step execution timeout in seconds. Dictionary build requires "
            "10-15 minutes for ontology API calls. Default 300s (5min) is "
            "too short; recommend 1200s (20min) for production builds."
        ),
    )
    taxonomy_subtree_root: int | None = Field(
        default=10239,
        description=(
            "NCBI taxon id used as the root for the names.dmp synthesis "
            "pass (SC-A4). Default 10239 = Viruses subtree per Q1 of "
            "SYNONYM_COMPLETENESS_PLAN.md. Set to None to disable the "
            "synthesis pass entirely (corpus-only build, pre-SC-A4 "
            "behaviour). Setting to 1 (root of life) would balloon the "
            "SQLite to ~50x its virus-subtree size — discouraged."
        ),
    )


class DictionaryBuildStep(BaseStep):
    """Build the synonym-dictionary artifact from the configured input
    tables, embedding the NCBITaxon hierarchy from the upstream taxdump.

    Expected ``process()`` input::

        {"taxdump_paths": {
            "nodes_path": "/abs/path/to/nodes.dmp",
            "merged_path": "/abs/path/to/merged.dmp",
        }}

    Return shape::

        {"build_result": {
            "sqlite_path": "/abs/path/to/dictionary.sqlite",
            "manifest": {<BuildManifest.model_dump(mode='json')>},
        }}
    """

    COMPONENT_TYPE: str = "dictionary_build_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return DictionaryBuildStepConfig

    @classmethod
    def extract_component_config(cls, config: DictionaryBuildStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "violin_pathogens_path": config.violin_pathogens_path,
            "violin_vaccines_path": config.violin_vaccines_path,
            "violin_genes_path": config.violin_genes_path,
            "bvbrc_genomes_path": config.bvbrc_genomes_path,
            "output_dir": config.output_dir,
            "dictionary_version": config.dictionary_version,
            "ncbitaxon_version": config.ncbitaxon_version,
            "vo_version": config.vo_version,
            "doid_version": config.doid_version,
            "ncbigene_version": config.ncbigene_version,
            "max_rows": config.max_rows,
            "taxonomy_subtree_root": config.taxonomy_subtree_root,
        }

    def _init_from_config(
        self,
        config: DictionaryBuildStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._violin_pathogens_path: str | None = component_config.get("violin_pathogens_path")
        self._violin_vaccines_path: str | None = component_config.get("violin_vaccines_path")
        self._violin_genes_path: str | None = component_config.get("violin_genes_path")
        self._bvbrc_genomes_path: str | None = component_config.get("bvbrc_genomes_path")
        self._output_dir: str = component_config["output_dir"]
        self._dictionary_version: str = component_config["dictionary_version"]
        self._ontology_versions: dict[OntologyName, str] = {
            OntologyName.NCBITAXON: component_config["ncbitaxon_version"],
            OntologyName.VO: component_config["vo_version"],
            OntologyName.DOID: component_config["doid_version"],
            OntologyName.NCBIGENE: component_config["ncbigene_version"],
        }
        self._max_rows: int | None = component_config.get("max_rows")
        self._taxonomy_subtree_root: int | None = component_config.get("taxonomy_subtree_root")

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        taxdump_paths = self._extract_taxdump_paths(input_data)

        out_root = Path(self._output_dir).expanduser().resolve()
        enriched_dir = out_root / "enriched"
        output_dictionary = out_root / "dictionary.sqlite"

        specs = self._make_table_specs(enriched_dir=enriched_dir)
        if not specs:
            raise ValueError(
                f"DictionaryBuildStep '{self.name}': at least one of "
                "violin_pathogens_path / violin_vaccines_path / "
                "violin_genes_path / bvbrc_genomes_path must be set."
            )

        if self._max_rows is not None:
            specs = self._truncate_inputs(specs, self._max_rows)

        log.info(
            "DictionaryBuildStep %s: building dictionary at %s (version=%s, %d table(s))",
            self.name,
            output_dictionary,
            self._dictionary_version,
            len(specs),
        )

        # names.dmp / delnodes.dmp are SC-A2 additions; older callers that
        # haven't refreshed their taxdump cache will omit them. Pass them
        # through only when present and non-empty.
        names_path_str = taxdump_paths.get("names_path")
        delnodes_path_str = taxdump_paths.get("delnodes_path")

        manifest = await build_dictionary(
            table_specs=specs,
            output_dictionary=output_dictionary,
            dictionary_version=self._dictionary_version,
            ontology_versions=self._ontology_versions,
            writer_factory=SQLiteDictionaryWriter,
            nodes_dmp_path=Path(taxdump_paths["nodes_path"]),
            merged_dmp_path=Path(taxdump_paths["merged_path"]),
            names_dmp_path=Path(names_path_str) if names_path_str else None,
            delnodes_dmp_path=Path(delnodes_path_str) if delnodes_path_str else None,
            taxonomy_subtree_root=(self._taxonomy_subtree_root if names_path_str else None),
        )

        # Mirror the legacy CLI: write a JSON copy of the manifest for
        # human inspection.
        manifest_json_path = out_root / "manifest.json"
        manifest_json_path.write_text(manifest.model_dump_json(indent=2))

        result = {
            "sqlite_path": str(output_dictionary),
            "manifest": manifest.model_dump(mode="json"),
        }
        log.info(
            "DictionaryBuildStep %s: build complete; %d rows, %d unresolved",
            self.name,
            manifest.record_count_total,
            manifest.unresolved_count,
        )
        return {"build_result": result}

    @staticmethod
    def _extract_taxdump_paths(input_data: dict[str, Any]) -> dict[str, str]:
        """Pull the four taxdump file paths out of the input.

        Required: ``nodes_path``, ``merged_path``.
        Optional (SC-A2 additions, 2026-06-08): ``names_path``,
        ``delnodes_path``. When present, the build runs the names.dmp
        synthesis pass (SC-A4) and writes the deleted-taxons table.
        Absent keys are returned simply as missing — the build downgrades
        to corpus-only behaviour and logs the omission. We do this
        rather than fail hard so a pre-SC-A2 cached taxdump still
        produces a working (but smaller-coverage) dictionary.

        Accepts both ``input_data["taxdump_paths"]`` (canonical) and a
        bare ``{"nodes_path": ..., "merged_path": ...}`` dict (legacy
        passthrough when the framework hands us the data-unit value
        directly).
        """
        candidate = input_data.get("taxdump_paths", input_data)
        if not isinstance(candidate, dict):
            raise ValueError(
                "DictionaryBuildStep: expected input to contain "
                "'taxdump_paths' as a dict with 'nodes_path' and "
                f"'merged_path' keys; got {type(candidate).__name__}."
            )
        nodes_path = candidate.get("nodes_path")
        merged_path = candidate.get("merged_path")
        if not isinstance(nodes_path, str) or not isinstance(merged_path, str):
            raise ValueError(
                "DictionaryBuildStep: 'taxdump_paths' must contain "
                "'nodes_path' and 'merged_path' as strings; got "
                f"nodes_path={type(nodes_path).__name__}, "
                f"merged_path={type(merged_path).__name__}."
            )
        result: dict[str, str] = {
            "nodes_path": nodes_path,
            "merged_path": merged_path,
        }
        # Optional SC-A2 additions; type-check defensively to avoid
        # propagating a non-string through to ``Path(...)``.
        for key in ("names_path", "delnodes_path"):
            value = candidate.get(key)
            if isinstance(value, str) and value:
                result[key] = value
        return result

    def _make_table_specs(self, *, enriched_dir: Path) -> list[TableSpec]:
        """Equivalent of cli._make_table_specs but driven by step config.

        Skips any table whose configured path is ``None``. Resolver
        choice mirrors the legacy CLI exactly:

        - violin_pathogens / bvbrc_genomes → :class:`PathogenResolver`
        - violin_vaccines → :class:`VaccineResolver`
        - violin_genes → :class:`GeneResolver`
        """
        specs: list[TableSpec] = []
        if self._violin_pathogens_path:
            specs.append(
                TableSpec(
                    name="violin.pathogen",
                    input_path=Path(self._violin_pathogens_path),
                    output_path=enriched_dir / "violin_pathogens_enriched.csv",
                    resolver_factory=lambda c, v: PathogenResolver(c, dictionary_version=v),
                    sep=",",
                )
            )
        if self._violin_vaccines_path:
            specs.append(
                TableSpec(
                    name="violin.vaccine",
                    input_path=Path(self._violin_vaccines_path),
                    output_path=enriched_dir / "violin_vaccines_enriched.csv",
                    resolver_factory=lambda c, v: VaccineResolver(c, dictionary_version=v),
                    sep=",",
                )
            )
        if self._violin_genes_path:
            specs.append(
                TableSpec(
                    name="violin.gene",
                    input_path=Path(self._violin_genes_path),
                    output_path=enriched_dir / "violin_genes_enriched.csv",
                    resolver_factory=lambda c, v: GeneResolver(c, dictionary_version=v),
                    sep=",",
                )
            )
        if self._bvbrc_genomes_path:
            specs.append(
                TableSpec(
                    name="bvbrc.genome",
                    input_path=Path(self._bvbrc_genomes_path),
                    output_path=enriched_dir / "bvbrc_genomes_enriched.csv",
                    resolver_factory=lambda c, v: PathogenResolver(c, dictionary_version=v),
                    sep="\t",
                )
            )
        return specs

    @staticmethod
    def _truncate_inputs(specs: list[TableSpec], max_rows: int) -> list[TableSpec]:
        """Mirror cli._truncate_inputs — write a truncated copy of any
        input larger than ``max_rows`` and rewrite the spec to point
        at the truncated file. Keeps build_dictionary ignorant of the
        cap.
        """
        import pandas as pd  # noqa: PLC0415 — pandas is heavyweight; lazy-import

        truncated: list[TableSpec] = []
        for s in specs:
            df = pd.read_csv(s.input_path, sep=s.sep, low_memory=False)
            if len(df) <= max_rows:
                truncated.append(s)
                continue
            tmp_dir = s.output_path.parent / "_truncated"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = tmp_dir / s.input_path.name
            df.head(max_rows).to_csv(tmp_path, sep=s.sep, index=False)
            truncated.append(
                TableSpec(
                    name=s.name,
                    input_path=tmp_path,
                    output_path=s.output_path,
                    resolver_factory=s.resolver_factory,
                    sep=s.sep,
                )
            )
        return truncated
