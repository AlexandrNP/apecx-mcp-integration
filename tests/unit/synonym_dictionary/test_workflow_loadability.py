"""Loadability pinning for the dictionary-build workflow YAMLs.

Cheap correctness gate — verifies that the three new YAMLs (taxdump
fetch step, dictionary build step, top-level workflow) load via
``BaseStep.from_config`` / ``Workflow.from_config`` without error.

Does NOT make any live OLS calls, hit NCBI, or shell out to any
external system. Pure shape verification:

- The class paths in the YAMLs resolve to importable Python.
- The Pydantic step config classes accept the YAML fields (and reject
  typos via ``extra='forbid'``).
- The workflow YAML's link source/target dot-references are valid.

Live-OLS / live-NCBI integration tests live in
``tests/integration/test_synonym_*`` and are migrated to drive this
workflow in a later phase.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

CONFIGS_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "apecx_integration"
    / "synonym_dictionary"
    / "workflow"
    / "configs"
)


def test_taxdump_fetch_step_yaml_loads() -> None:
    """``TaxdumpFetchStep`` YAML loads cleanly via ``from_config``."""
    from apecx_integration.synonym_dictionary.workflow.taxdump_fetch_step import (
        TaxdumpFetchStep,
    )

    path = CONFIGS_DIR / "taxdump_fetch_step.yml"
    assert path.is_file(), path

    step = TaxdumpFetchStep.from_config(str(path))

    assert step.name == "taxdump_fetch_step"
    # Defaults from TaxdumpFetchStepConfig (env var not set in this test).
    assert isinstance(step._output_dir, str)
    assert step._output_dir, "output_dir must default to a non-empty string"
    assert step._url is None
    assert step._force is False


def test_dictionary_build_step_yaml_loads() -> None:
    """``DictionaryBuildStep`` YAML loads cleanly via ``from_config``."""
    from apecx_integration.synonym_dictionary.workflow.dictionary_build_step import (
        DictionaryBuildStep,
    )

    path = CONFIGS_DIR / "dictionary_build_step.yml"
    assert path.is_file(), path

    step = DictionaryBuildStep.from_config(str(path))

    assert step.name == "dictionary_build_step"
    # All four input-table paths default to None (none specified in the YAML).
    assert step._violin_pathogens_path is None
    assert step._violin_vaccines_path is None
    assert step._violin_genes_path is None
    assert step._bvbrc_genomes_path is None
    # output_dir defaults via env-var fallback to a non-empty string.
    assert isinstance(step._output_dir, str)
    assert step._output_dir
    # Ontology version pins default to "unknown".
    assert (
        step._ontology_versions[
            __import__(
                "apecx_integration.synonym_dictionary.enums",
                fromlist=["OntologyName"],
            ).OntologyName.NCBITAXON
        ]
        == "unknown"
    )
    # max_rows defaults to None.
    assert step._max_rows is None
    # dictionary_version defaults to a non-empty ISO-shaped string.
    assert isinstance(step._dictionary_version, str)
    assert step._dictionary_version


def test_dictionary_build_workflow_yaml_loads() -> None:
    """The top-level workflow YAML loads cleanly via ``Workflow.from_config``.

    Confirms:
    - Both steps wire via ``class:`` + ``config:`` references.
    - The three DirectLinks parse with ``source``/``target`` dot
      references and ``auto_transfer: true``.
    """
    from nanobrain.core.workflow import Workflow

    path = CONFIGS_DIR / "dictionary_build_workflow.yml"
    assert path.is_file(), path

    workflow = Workflow.from_config(str(path))

    # Workflow has both child steps under the expected names.
    assert "taxdump_fetch" in workflow.child_steps
    assert "dictionary_build" in workflow.child_steps

    # Workflow owns three DirectLinks.
    assert len(workflow.step_links) == 3, (
        f"Expected 3 links; got {list(workflow.step_links.keys())}"
    )

    # Every link has auto_transfer=True (workspace policy: the False
    # default is a silent-failure shape).
    for link_name, link in workflow.step_links.items():
        assert getattr(link, "auto_transfer", False) is True, (
            f"Link {link_name!r} has auto_transfer={getattr(link, 'auto_transfer', None)}; "
            "the workflow YAML must set auto_transfer: true on every link."
        )


def test_taxdump_fetch_step_config_rejects_unknown_field(tmp_path: Path) -> None:
    """``extra='forbid'`` on TaxdumpFetchStepConfig surfaces YAML typos.

    Loads a tweaked YAML with a misspelled field (``out_dir`` instead of
    ``output_dir``) and asserts that ``from_config`` raises rather than
    silently ignoring the typo and using the default. This is the rule
    workspace memory ``pydantic_extra_forbid_rule.md`` exists to enforce.
    """
    from apecx_integration.synonym_dictionary.workflow.taxdump_fetch_step import (
        TaxdumpFetchStep,
    )

    bad_yaml = tmp_path / "taxdump_fetch_step_typo.yml"
    bad_yaml.write_text(
        'class: "apecx_integration.synonym_dictionary.workflow.taxdump_fetch_step.TaxdumpFetchStep"\n'
        "name: taxdump_fetch_step\n"
        "out_dir: /tmp/typo\n"
        "input_data_units:\n"
        "  trigger:\n"
        '    class: "nanobrain.core.data_unit.DataUnitMemory"\n'
        "    name: trigger\n"
        "output_data_units:\n"
        "  taxdump_paths:\n"
        '    class: "nanobrain.core.data_unit.DataUnitMemory"\n'
        "    name: taxdump_paths\n"
        "triggers:\n"
        '  - class: "nanobrain.core.trigger.DataUnitChangeTrigger"\n'
        '    data_unit: "trigger"\n'
    )

    with pytest.raises(Exception) as exc_info:
        TaxdumpFetchStep.from_config(str(bad_yaml))
    # Pydantic raises ValidationError; the framework may re-wrap.
    # Either way the error mentions the unknown field.
    assert "out_dir" in str(exc_info.value)
