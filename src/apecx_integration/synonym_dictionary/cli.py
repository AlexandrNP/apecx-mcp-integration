"""Local-first CLI for Stage 1 dictionary builds.

Usage:

.. code-block:: shell

    apecx-build-dictionary \\
        --violin-pathogens data/violin/Pathogen_Information.csv \\
        --violin-vaccines data/violin/Vaccine_Information.csv \\
        --bvbrc-genomes data/bvbrc_cache/alphavirus_genomes.tsv \\
        --output build/dictionary

Outputs:

- ``build/dictionary/dictionary.sqlite``
- ``build/dictionary/enriched/<table>.csv``
- ``build/dictionary/manifest.json`` (a JSON copy of the manifest, for
  human inspection — the canonical copy lives inside the SQLite).

Per Phase 5 of the v5 plan, this CLI is the local-first entry point.
At Phase 6 the same logic is wrapped into a harvester ``Transform``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from apecx_integration.synonym_dictionary.build import (
    TableSpec,
    run_build_sync,
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


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apecx-build-dictionary",
        description=(
            "Build a synonym dictionary artifact from local VIOLIN / "
            "BV-BRC snapshots.  Stage 1 of the ontology-integration plan."
        ),
    )
    parser.add_argument(
        "--violin-pathogens",
        type=Path,
        default=None,
        help="Path to VIOLIN Pathogen_Information.csv",
    )
    parser.add_argument(
        "--violin-vaccines",
        type=Path,
        default=None,
        help="Path to VIOLIN Vaccine_Information.csv",
    )
    parser.add_argument(
        "--violin-genes",
        type=Path,
        default=None,
        help=(
            "Path to VIOLIN Gene_Information.csv.  Resolves via NCBI_Gene_ID "
            "to identifiers.org/ncbigene/ IRIs; no OLS calls needed."
        ),
    )
    parser.add_argument(
        "--bvbrc-genomes",
        type=Path,
        default=None,
        help=(
            "Path to a BV-BRC genomes TSV (e.g. alphavirus_genomes.tsv). "
            "Genome rows resolve via implicit NCBITaxon (genome_id prefix)."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output directory.  Will contain dictionary.sqlite and enriched/ CSVs.",
    )
    parser.add_argument(
        "--dictionary-version",
        default=None,
        help="Build identifier; defaults to ISO timestamp.",
    )
    parser.add_argument(
        "--ncbitaxon-version",
        default="unknown",
        help="Pinned NCBITaxon release identifier (recorded in manifest).",
    )
    parser.add_argument(
        "--vo-version",
        default="unknown",
        help="Pinned Vaccine Ontology release identifier.",
    )
    parser.add_argument(
        "--doid-version",
        default="unknown",
        help="Pinned Disease Ontology release identifier.",
    )
    parser.add_argument(
        "--ncbigene-version",
        default="unknown",
        help=(
            "NCBI Gene build identifier (e.g. '2026-04-01').  "
            "Recorded in the manifest; no OLS release to pin against."
        ),
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help=(
            "Optional cap on rows-per-table.  Useful for smoke-testing "
            "against live OLS without doing a full build."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser


def _make_table_specs(args: argparse.Namespace) -> list[TableSpec]:
    out_root = args.output
    enriched_dir = out_root / "enriched"
    specs: list[TableSpec] = []

    if args.violin_pathogens:
        specs.append(
            TableSpec(
                name="violin.pathogen",
                input_path=args.violin_pathogens,
                output_path=enriched_dir / "violin_pathogens_enriched.csv",
                resolver_factory=lambda c, v: PathogenResolver(c, dictionary_version=v),
                sep=",",
            )
        )
    if args.violin_vaccines:
        specs.append(
            TableSpec(
                name="violin.vaccine",
                input_path=args.violin_vaccines,
                output_path=enriched_dir / "violin_vaccines_enriched.csv",
                resolver_factory=lambda c, v: VaccineResolver(c, dictionary_version=v),
                sep=",",
            )
        )
    if args.violin_genes:
        specs.append(
            TableSpec(
                name="violin.gene",
                input_path=args.violin_genes,
                output_path=enriched_dir / "violin_genes_enriched.csv",
                resolver_factory=lambda c, v: GeneResolver(c, dictionary_version=v),
                sep=",",
            )
        )
    if args.bvbrc_genomes:
        specs.append(
            TableSpec(
                name="bvbrc.genome",
                input_path=args.bvbrc_genomes,
                output_path=enriched_dir / "bvbrc_genomes_enriched.csv",
                resolver_factory=lambda c, v: PathogenResolver(c, dictionary_version=v),
                sep="\t",
            )
        )
    return specs


def _truncate_inputs(specs: list[TableSpec], max_rows: int) -> list[TableSpec]:
    """When --max-rows is given, write a truncated copy to a tmp file and
    rewrite the spec to point at it.  Keeps the build pipeline ignorant
    of the cap."""
    import pandas as pd

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


def main(argv: list[str] | None = None) -> int:
    parser = _build_argparser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    specs = _make_table_specs(args)
    if not specs:
        print(
            "error: at least one of --violin-pathogens / --violin-vaccines / "
            "--violin-genes / --bvbrc-genomes is required",
            file=sys.stderr,
        )
        return 2

    if args.max_rows is not None:
        specs = _truncate_inputs(specs, args.max_rows)

    dictionary_version = args.dictionary_version or datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    output_root = args.output
    output_dictionary = output_root / "dictionary.sqlite"

    ontology_versions = {
        OntologyName.NCBITAXON: args.ncbitaxon_version,
        OntologyName.VO: args.vo_version,
        OntologyName.DOID: args.doid_version,
        OntologyName.NCBIGENE: args.ncbigene_version,
    }

    manifest = run_build_sync(
        table_specs=specs,
        output_dictionary=output_dictionary,
        dictionary_version=dictionary_version,
        ontology_versions=ontology_versions,
        writer_factory=SQLiteDictionaryWriter,
    )

    # Human-inspectable manifest copy.
    manifest_json_path = output_root / "manifest.json"
    manifest_json_path.write_text(manifest.model_dump_json(indent=2))

    print(
        f"build complete: {manifest.record_count_total} rows, "
        f"{manifest.unresolved_count} unresolved, "
        f"dictionary at {output_dictionary}, manifest at {manifest_json_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
