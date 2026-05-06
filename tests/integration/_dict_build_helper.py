"""Test-only helper that drives the dictionary-build workflow with explicit
per-test inputs. Replacement for the deleted ``apecx_integration.synonym_dictionary.cli``.

Why this helper exists
----------------------
The ``apecx-build-dictionary`` console script (and its programmatic entry
point ``synonym_dictionary.cli.main``) was deleted on the
``dictionary-build-as-workflow`` branch. The replacement is the
:class:`DictionaryBuildStep` + :class:`TaxdumpFetchStep` pair, driven
either by the ``dictionary_build_workflow.yml`` (production path) or by
the :func:`apecx_integration.synonym_dictionary.workflow.bootstrap.ensure_dictionary`
function (lazy-at-MCP-startup path).

Neither the workflow YAML nor the bootstrap exposes a clean way to pass
arbitrary VIOLIN/BV-BRC table paths, a ``--max-rows`` slice, or a custom
``--dictionary-version`` from a test — those used to flow through CLI
flags. Rather than grow the bootstrap surface for a feature only tests
need, this helper writes a per-test temporary step YAML, instantiates
both steps via the canonical ``BaseStep.from_config(<path>)`` pattern,
and runs them in sequence. It bypasses the trigger cascade and the
workflow YAML entirely — option (1) from the migration task spec.

Usage
-----
::

    from tests.integration._dict_build_helper import build_dictionary_for_test

    db_path = build_dictionary_for_test(
        output_dir=tmp_path / "dict",
        dictionary_version="test-foo",
        max_rows=5,
        violin_pathogens=VIOLIN_PATHOGENS,
        violin_genes=VIOLIN_GENES,         # optional
        violin_vaccines=None,              # optional
        bvbrc_genomes=None,                # optional
        nodes_dmp=None,                    # optional pre-fetched taxdump
        merged_dmp=None,                   # optional pre-fetched taxdump
        taxdump_dir=None,                  # optional override; default = tmp dir
    )
    # db_path == output_dir / "dictionary.sqlite"

Design notes
------------
* Each invocation creates its own temporary directory under
  ``output_dir / "_dictbuild_helper_yamls"`` for the two step YAMLs so
  ``BaseStep.from_config(<file>)`` is satisfied (the framework refuses
  inline-dict StepConfigs — see component_base.py).
* The taxdump step is run first; its returned ``{nodes_path,
  merged_path}`` dict is fed directly into the dictionary-build step's
  ``process()`` call. No DataUnit plumbing, no trigger cascade — that
  is exercised by ``test_iri_resolution_workflow.py`` and the
  workflow-YAML loadability tests.
* If pre-fetched taxdump files are supplied via ``nodes_dmp`` /
  ``merged_dmp``, the taxdump step is skipped entirely. This is what
  ``test_taxdump_real_hierarchy.py`` needs to avoid re-downloading the
  ~72 MB archive on every run.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path
from typing import Any


def _write_taxdump_step_yaml(yaml_dir: Path, output_dir: Path) -> Path:
    """Emit a minimal TaxdumpFetchStep YAML with the requested output_dir."""
    path = yaml_dir / "taxdump_fetch_step.yml"
    path.write_text(
        textwrap.dedent(
            f"""\
            name: taxdump_fetch_step
            description: "Test-helper taxdump fetch."
            output_dir: "{output_dir}"
            input_data_units:
              trigger:
                class: "nanobrain.core.data_unit.DataUnitMemory"
                name: trigger
                persistent: false
            output_data_units:
              taxdump_paths:
                class: "nanobrain.core.data_unit.DataUnitMemory"
                name: taxdump_paths
                persistent: false
            triggers:
              - class: "nanobrain.core.trigger.DataUnitChangeTrigger"
                data_unit: "trigger"
            """
        )
    )
    return path


def _write_dictbuild_step_yaml(
    yaml_dir: Path,
    *,
    output_dir: Path,
    dictionary_version: str,
    max_rows: int | None,
    violin_pathogens: Path | None,
    violin_vaccines: Path | None,
    violin_genes: Path | None,
    bvbrc_genomes: Path | None,
) -> Path:
    """Emit a DictionaryBuildStep YAML with explicit table paths."""
    path = yaml_dir / "dictionary_build_step.yml"
    lines: list[str] = [
        "name: dictionary_build_step",
        'description: "Test-helper dictionary build."',
        f'output_dir: "{output_dir}"',
        f'dictionary_version: "{dictionary_version}"',
    ]
    if max_rows is not None:
        lines.append(f"max_rows: {int(max_rows)}")
    if violin_pathogens is not None:
        lines.append(f'violin_pathogens_path: "{violin_pathogens}"')
    if violin_vaccines is not None:
        lines.append(f'violin_vaccines_path: "{violin_vaccines}"')
    if violin_genes is not None:
        lines.append(f'violin_genes_path: "{violin_genes}"')
    if bvbrc_genomes is not None:
        lines.append(f'bvbrc_genomes_path: "{bvbrc_genomes}"')
    lines.extend(
        [
            "input_data_units:",
            "  taxdump_paths:",
            '    class: "nanobrain.core.data_unit.DataUnitMemory"',
            "    name: taxdump_paths",
            "    persistent: false",
            "output_data_units:",
            "  build_result:",
            '    class: "nanobrain.core.data_unit.DataUnitMemory"',
            "    name: build_result",
            "    persistent: false",
            "triggers:",
            '  - class: "nanobrain.core.trigger.DataUnitChangeTrigger"',
            '    data_unit: "taxdump_paths"',
        ]
    )
    path.write_text("\n".join(lines) + "\n")
    return path


async def _drive_build(
    *,
    yaml_dir: Path,
    output_dir: Path,
    dictionary_version: str,
    max_rows: int | None,
    violin_pathogens: Path | None,
    violin_vaccines: Path | None,
    violin_genes: Path | None,
    bvbrc_genomes: Path | None,
    taxdump_dir: Path,
    nodes_dmp: Path | None,
    merged_dmp: Path | None,
) -> Path:
    # Imported inside the helper so importing this module doesn't pull in
    # the full nanobrain framework just to read the docstring.
    from apecx_integration.synonym_dictionary.workflow.dictionary_build_step import (
        DictionaryBuildStep,
    )
    from apecx_integration.synonym_dictionary.workflow.taxdump_fetch_step import (
        TaxdumpFetchStep,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    yaml_dir.mkdir(parents=True, exist_ok=True)

    if nodes_dmp is not None and merged_dmp is not None:
        taxdump_paths: dict[str, str] = {
            "nodes_path": str(nodes_dmp),
            "merged_path": str(merged_dmp),
        }
    else:
        taxdump_dir.mkdir(parents=True, exist_ok=True)
        taxdump_yaml = _write_taxdump_step_yaml(yaml_dir, taxdump_dir)
        taxdump_step = TaxdumpFetchStep.from_config(str(taxdump_yaml))
        await taxdump_step.initialize()
        taxdump_result: dict[str, Any] = await taxdump_step.process({"trigger": True})
        taxdump_paths = taxdump_result["taxdump_paths"]

    dict_yaml = _write_dictbuild_step_yaml(
        yaml_dir,
        output_dir=output_dir,
        dictionary_version=dictionary_version,
        max_rows=max_rows,
        violin_pathogens=violin_pathogens,
        violin_vaccines=violin_vaccines,
        violin_genes=violin_genes,
        bvbrc_genomes=bvbrc_genomes,
    )
    dict_step = DictionaryBuildStep.from_config(str(dict_yaml))
    await dict_step.initialize()
    build_result = await dict_step.process({"taxdump_paths": taxdump_paths})

    sqlite_path = Path(build_result["build_result"]["sqlite_path"])
    if not sqlite_path.is_file():
        raise RuntimeError(
            f"DictionaryBuildStep returned {sqlite_path} but the file does not exist; "
            "the underlying build_dictionary call failed silently."
        )
    return sqlite_path


def build_dictionary_for_test(
    *,
    output_dir: Path,
    dictionary_version: str,
    max_rows: int | None = None,
    violin_pathogens: Path | None = None,
    violin_vaccines: Path | None = None,
    violin_genes: Path | None = None,
    bvbrc_genomes: Path | None = None,
    nodes_dmp: Path | None = None,
    merged_dmp: Path | None = None,
    taxdump_dir: Path | None = None,
) -> Path:
    """Drive TaxdumpFetchStep + DictionaryBuildStep against explicit inputs.

    Returns the absolute path to the built ``dictionary.sqlite``.

    All keyword arguments are required to be passed by name to make the
    call site self-documenting — the legacy CLI accepted positional
    flags and the pattern is easy to misread otherwise.
    """
    output_dir = Path(output_dir)
    yaml_dir = output_dir / "_dictbuild_helper_yamls"
    if taxdump_dir is None:
        taxdump_dir = output_dir / "_taxdump"

    return asyncio.run(
        _drive_build(
            yaml_dir=yaml_dir,
            output_dir=output_dir,
            dictionary_version=dictionary_version,
            max_rows=max_rows,
            violin_pathogens=violin_pathogens,
            violin_vaccines=violin_vaccines,
            violin_genes=violin_genes,
            bvbrc_genomes=bvbrc_genomes,
            taxdump_dir=taxdump_dir,
            nodes_dmp=nodes_dmp,
            merged_dmp=merged_dmp,
        )
    )
