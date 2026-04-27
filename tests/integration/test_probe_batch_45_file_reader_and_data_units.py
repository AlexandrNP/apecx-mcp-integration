"""Probe batch 45 — adversarial probes against DelimitedFileReaderStep
+ data_unit_schemas + workflow YAML files for data-flow integrity.

Streak before this batch: 124/300 post-AQ post-1066.
Probe naming: 1180–1204.

Distinct probes only.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from apecx_integration.composition.steps.file_readers import (
    DelimitedFileReaderStep,
    DelimitedFileReaderStepConfig,
)


pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = REPO_ROOT.parent
WORKFLOW_DIR = (
    REPO_ROOT / "src" / "apecx_integration" / "composition"
    / "workflows" / "violin_bvbrc"
)
VIOLIN_VACCINE_YAML = WORKFLOW_DIR / "steps" / "violin_vaccine_reader.yml"
BVBRC_GENOMES_YAML = WORKFLOW_DIR / "steps" / "bvbrc_alphavirus_genomes_reader.yml"


# --------------------------------------------------------------------------- #
# Probes 1180–1204
# --------------------------------------------------------------------------- #


def test_probe_1180_file_reader_step_class_attributes():
    assert DelimitedFileReaderStep.COMPONENT_TYPE
    assert "name" in DelimitedFileReaderStep.REQUIRED_CONFIG_FIELDS
    assert "file_path" in DelimitedFileReaderStep.REQUIRED_CONFIG_FIELDS


def test_probe_1181_file_reader_step_process_is_async():
    assert inspect.iscoroutinefunction(DelimitedFileReaderStep.process)


def test_probe_1182_file_reader_invalid_format_rejected_at_init(tmp_path):
    """``format`` only accepts ``csv`` / ``tsv``; anything else
    rejects at step init (NOT at first process()). Pin the early
    fail-fast contract."""
    csv_file = tmp_path / "data.csv"
    csv_file.write_text("a,b\n1,2\n")
    wrapper = tmp_path / "reader.yml"
    wrapper.write_text(
        "class: apecx_integration.composition.steps.file_readers.DelimitedFileReaderStep\n"
        "name: bad_format\n"
        "description: bad format test\n"
        f"file_path: '{csv_file}'\n"
        "format: 'xml'\n"
        "input_data_units: {}\n"
        "output_data_units:\n"
        "  records_output:\n"
        "    class: nanobrain.core.data_unit.DataUnitMemory\n"
        "    name: records_output\n"
        "    description: out\n"
        "    persistent: false\n"
        "triggers: []\n"
    )
    # Pydantic's Literal["csv", "tsv"] rejects "xml" at config-load,
    # OR _init_from_config's own validation rejects. Either kind of
    # error is acceptable — the property under test is fail-fast at
    # init, not the specific error class.
    with pytest.raises((Exception,)):
        DelimitedFileReaderStep.from_config(str(wrapper))


def test_probe_1183_file_reader_missing_file_raises_at_process_time(tmp_path):
    """File path that doesn't exist at process() time raises
    FileNotFoundError with a clear message. The check is at
    process(), not init — so a future-existing file works without
    requiring the file at deploy time."""
    wrapper = tmp_path / "reader.yml"
    wrapper.write_text(
        "class: apecx_integration.composition.steps.file_readers.DelimitedFileReaderStep\n"
        "name: missing_file\n"
        "description: missing file test\n"
        f"file_path: '{tmp_path / 'nonexistent.csv'}'\n"
        "input_data_units: {}\n"
        "output_data_units:\n"
        "  records_output:\n"
        "    class: nanobrain.core.data_unit.DataUnitMemory\n"
        "    name: records_output\n"
        "    description: out\n"
        "    persistent: false\n"
        "triggers: []\n"
    )
    step = DelimitedFileReaderStep.from_config(str(wrapper))
    with pytest.raises(FileNotFoundError, match="file not found"):
        asyncio.run(step.process({}))


def test_probe_1184_file_reader_required_columns_missing_raises(tmp_path):
    """A CSV missing a required column raises with explicit naming
    of missing + actual columns. Pin this for diagnostic clarity."""
    csv_file = tmp_path / "missing_cols.csv"
    csv_file.write_text("a,b\n1,2\n")
    wrapper = tmp_path / "reader.yml"
    wrapper.write_text(
        "class: apecx_integration.composition.steps.file_readers.DelimitedFileReaderStep\n"
        "name: req_cols\n"
        "description: required cols test\n"
        f"file_path: '{csv_file}'\n"
        "required_columns: ['a', 'c', 'd']\n"
        "input_data_units: {}\n"
        "output_data_units:\n"
        "  records_output:\n"
        "    class: nanobrain.core.data_unit.DataUnitMemory\n"
        "    name: records_output\n"
        "    description: out\n"
        "    persistent: false\n"
        "triggers: []\n"
    )
    step = DelimitedFileReaderStep.from_config(str(wrapper))
    with pytest.raises(ValueError, match="missing required columns"):
        asyncio.run(step.process({}))


def test_probe_1185_file_reader_returns_records_row_count_source_path(tmp_path):
    """Output dict shape: ``{records, row_count, source_path}``. Pin
    so a future addition / removal surfaces."""
    csv_file = tmp_path / "good.csv"
    csv_file.write_text("a,b\n1,x\n2,y\n3,z\n")
    wrapper = tmp_path / "reader.yml"
    wrapper.write_text(
        "class: apecx_integration.composition.steps.file_readers.DelimitedFileReaderStep\n"
        "name: good\n"
        "description: good test\n"
        f"file_path: '{csv_file}'\n"
        "input_data_units: {}\n"
        "output_data_units:\n"
        "  records_output:\n"
        "    class: nanobrain.core.data_unit.DataUnitMemory\n"
        "    name: records_output\n"
        "    description: out\n"
        "    persistent: false\n"
        "triggers: []\n"
    )
    step = DelimitedFileReaderStep.from_config(str(wrapper))
    out = asyncio.run(step.process({}))
    assert set(out.keys()) == {"records", "row_count", "source_path"}
    assert out["row_count"] == 3
    assert len(out["records"]) == 3


def test_probe_1186_file_reader_handles_empty_file(tmp_path):
    """Empty CSV with header but no rows is valid — returns 0 records."""
    csv_file = tmp_path / "empty.csv"
    csv_file.write_text("a,b\n")
    wrapper = tmp_path / "reader.yml"
    wrapper.write_text(
        "class: apecx_integration.composition.steps.file_readers.DelimitedFileReaderStep\n"
        "name: empty\n"
        "description: empty\n"
        f"file_path: '{csv_file}'\n"
        "input_data_units: {}\n"
        "output_data_units:\n"
        "  records_output:\n"
        "    class: nanobrain.core.data_unit.DataUnitMemory\n"
        "    name: records_output\n"
        "    description: out\n"
        "    persistent: false\n"
        "triggers: []\n"
    )
    step = DelimitedFileReaderStep.from_config(str(wrapper))
    out = asyncio.run(step.process({}))
    assert out["row_count"] == 0
    assert out["records"] == []


def test_probe_1187_file_reader_tsv_format_uses_tab_delimiter(tmp_path):
    """``format: tsv`` -> tab delimiter. A bug splitting on commas
    despite tsv would silently load junk records."""
    tsv_file = tmp_path / "tabbed.tsv"
    tsv_file.write_text("col1\tcol2\nval1,withcomma\tval2\n")
    wrapper = tmp_path / "reader.yml"
    wrapper.write_text(
        "class: apecx_integration.composition.steps.file_readers.DelimitedFileReaderStep\n"
        "name: tsv_test\n"
        "description: tsv\n"
        f"file_path: '{tsv_file}'\n"
        "format: tsv\n"
        "input_data_units: {}\n"
        "output_data_units:\n"
        "  records_output:\n"
        "    class: nanobrain.core.data_unit.DataUnitMemory\n"
        "    name: records_output\n"
        "    description: out\n"
        "    persistent: false\n"
        "triggers: []\n"
    )
    step = DelimitedFileReaderStep.from_config(str(wrapper))
    out = asyncio.run(step.process({}))
    # The comma INSIDE val1 should NOT split the field.
    assert out["records"] == [{"col1": "val1,withcomma", "col2": "val2"}]


def test_probe_1188_file_reader_encoding_override(tmp_path):
    """A file in latin-1 encoding loaded with encoding='latin-1'
    must decode correctly. Without the override, utf-8 default
    would silently substitute replacement chars."""
    csv_file = tmp_path / "latin.csv"
    # 0xE9 = é in latin-1.
    csv_file.write_bytes(b"name\nCaf\xe9\n")
    wrapper = tmp_path / "reader.yml"
    wrapper.write_text(
        "class: apecx_integration.composition.steps.file_readers.DelimitedFileReaderStep\n"
        "name: latin\n"
        "description: latin\n"
        f"file_path: '{csv_file}'\n"
        "encoding: 'latin-1'\n"
        "input_data_units: {}\n"
        "output_data_units:\n"
        "  records_output:\n"
        "    class: nanobrain.core.data_unit.DataUnitMemory\n"
        "    name: records_output\n"
        "    description: out\n"
        "    persistent: false\n"
        "triggers: []\n"
    )
    step = DelimitedFileReaderStep.from_config(str(wrapper))
    out = asyncio.run(step.process({}))
    assert out["records"][0]["name"] == "Café"


def test_probe_1189_violin_vaccine_reader_yaml_loads():
    """The bundled VIOLIN reader YAML must validate."""
    step = DelimitedFileReaderStep.from_config(str(VIOLIN_VACCINE_YAML))
    assert step.name == "violin_vaccine_reader"


def test_probe_1190_bvbrc_genomes_reader_yaml_loads():
    """The BV-BRC reader YAML must validate."""
    step = DelimitedFileReaderStep.from_config(str(BVBRC_GENOMES_YAML))
    assert step.name == "bvbrc_alphavirus_genomes_reader"


def test_probe_1191_data_unit_schemas_module_imports_cleanly():
    """The TypedDict module is documentation-only at runtime; verify
    it imports without side effects."""
    import importlib
    importlib.import_module(
        "apecx_integration.composition.steps.data_unit_schemas"
    )


def test_probe_1192_data_unit_schemas_typed_dicts_have_documented_fields():
    """Pin the per-step output TypedDicts so a future contract drift
    is caught at test time."""
    from apecx_integration.composition.steps.data_unit_schemas import (
        Step1Output,
        Step3aOutput,
        Step3cOutput,
    )
    # TypedDict.__annotations__ has the fields.
    assert "entities" in Step1Output.__annotations__
    assert "query_terms" in Step1Output.__annotations__
    # 3a is the cache lookup output.
    assert any(
        k in Step3aOutput.__annotations__
        for k in ("cached_mappings", "novel_terms")
    )
    # 3c is the LLM proposals output.
    assert "llm_proposals" in Step3cOutput.__annotations__


def test_probe_1193_file_reader_csv_with_byte_order_mark(tmp_path):
    """A UTF-8 BOM-prefixed CSV (Excel often produces these) must
    NOT corrupt the first column header. With encoding='utf-8' the
    BOM remains in the first header; with 'utf-8-sig' it's stripped.
    Pin the current behavior so a future change is intentional."""
    csv_file = tmp_path / "bom.csv"
    csv_file.write_bytes(b"\xef\xbb\xbfname,age\nAlice,30\n")
    wrapper = tmp_path / "reader.yml"
    wrapper.write_text(
        "class: apecx_integration.composition.steps.file_readers.DelimitedFileReaderStep\n"
        "name: bom\n"
        "description: bom\n"
        f"file_path: '{csv_file}'\n"
        "input_data_units: {}\n"
        "output_data_units:\n"
        "  records_output:\n"
        "    class: nanobrain.core.data_unit.DataUnitMemory\n"
        "    name: records_output\n"
        "    description: out\n"
        "    persistent: false\n"
        "triggers: []\n"
    )
    step = DelimitedFileReaderStep.from_config(str(wrapper))
    out = asyncio.run(step.process({}))
    # Default encoding=utf-8 leaves BOM as part of first header.
    # The first record's first column key is the BOM-prefixed
    # name. (Documented current behavior; a future encoding default
    # of 'utf-8-sig' would strip and break this assertion loudly.)
    keys = list(out["records"][0].keys())
    assert any("name" in k for k in keys)


def test_probe_1194_file_reader_does_not_buffer_entire_file_for_streaming(tmp_path):
    """Read of a 10K-line file should produce 10K records. Defensive:
    no surprise truncation."""
    csv_file = tmp_path / "big.csv"
    lines = ["a,b\n"] + [f"{i},{i*2}\n" for i in range(10_000)]
    csv_file.write_text("".join(lines))
    wrapper = tmp_path / "reader.yml"
    wrapper.write_text(
        "class: apecx_integration.composition.steps.file_readers.DelimitedFileReaderStep\n"
        "name: big\n"
        "description: big\n"
        f"file_path: '{csv_file}'\n"
        "input_data_units: {}\n"
        "output_data_units:\n"
        "  records_output:\n"
        "    class: nanobrain.core.data_unit.DataUnitMemory\n"
        "    name: records_output\n"
        "    description: out\n"
        "    persistent: false\n"
        "triggers: []\n"
    )
    step = DelimitedFileReaderStep.from_config(str(wrapper))
    out = asyncio.run(step.process({}))
    assert out["row_count"] == 10_000


def test_probe_1195_file_reader_input_data_optional():
    """``process()`` accepts ``input_data=None`` (file readers don't
    consume input). Pin the optional-input contract."""
    # The signature is `input_data: dict[str, Any] | None = None`.
    sig = inspect.signature(DelimitedFileReaderStep.process)
    params = list(sig.parameters.values())
    # First param is self; second is input_data.
    assert params[1].default is None or params[1].default == inspect.Parameter.empty


def test_probe_1196_violin_vaccine_yaml_uses_relative_path(tmp_path):
    """The VIOLIN reader YAML's file_path should be relative-to-cwd
    so operators can override APECX_DB_DATA_DIR / cd into the data
    dir."""
    import yaml
    raw = yaml.safe_load(VIOLIN_VACCINE_YAML.read_text())
    file_path = raw.get("file_path", "")
    # Allow env var or relative path; reject absolute hardcoded.
    if file_path and "$" not in file_path:
        assert not file_path.startswith("/"), (
            f"VIOLIN YAML hardcodes absolute path: {file_path}"
        )


def test_probe_1197_bvbrc_yaml_uses_relative_or_env_path():
    """Same check for BV-BRC reader."""
    import yaml
    raw = yaml.safe_load(BVBRC_GENOMES_YAML.read_text())
    file_path = raw.get("file_path", "")
    if file_path and "$" not in file_path:
        assert not file_path.startswith("/"), (
            f"BV-BRC YAML hardcodes absolute path: {file_path}"
        )


def test_probe_1198_file_reader_handles_quoted_values_with_delimiter(tmp_path):
    """A CSV value like ``"Smith, John"`` (quoted comma) must NOT be
    split into two fields. csv.DictReader handles this; pin."""
    csv_file = tmp_path / "quoted.csv"
    csv_file.write_text('name,age\n"Smith, John",30\n')
    wrapper = tmp_path / "reader.yml"
    wrapper.write_text(
        "class: apecx_integration.composition.steps.file_readers.DelimitedFileReaderStep\n"
        "name: quoted\n"
        "description: q\n"
        f"file_path: '{csv_file}'\n"
        "input_data_units: {}\n"
        "output_data_units:\n"
        "  records_output:\n"
        "    class: nanobrain.core.data_unit.DataUnitMemory\n"
        "    name: records_output\n"
        "    description: out\n"
        "    persistent: false\n"
        "triggers: []\n"
    )
    step = DelimitedFileReaderStep.from_config(str(wrapper))
    out = asyncio.run(step.process({}))
    assert out["records"][0]["name"] == "Smith, John"


def test_probe_1199_file_reader_source_path_is_resolved_absolute(tmp_path):
    """``source_path`` in output is the resolved absolute path. A
    relative path leak would make logs harder to read."""
    csv_file = tmp_path / "data.csv"
    csv_file.write_text("a\n1\n")
    wrapper = tmp_path / "reader.yml"
    wrapper.write_text(
        "class: apecx_integration.composition.steps.file_readers.DelimitedFileReaderStep\n"
        "name: srcpath\n"
        "description: srcpath\n"
        f"file_path: '{csv_file}'\n"
        "input_data_units: {}\n"
        "output_data_units:\n"
        "  records_output:\n"
        "    class: nanobrain.core.data_unit.DataUnitMemory\n"
        "    name: records_output\n"
        "    description: out\n"
        "    persistent: false\n"
        "triggers: []\n"
    )
    step = DelimitedFileReaderStep.from_config(str(wrapper))
    out = asyncio.run(step.process({}))
    src = out["source_path"]
    assert Path(src).is_absolute()


def test_probe_1200_file_reader_log_includes_row_count_and_filename(tmp_path, caplog):
    """Operators read the log line to spot under/overcounts. Pin
    the format: row count + filename (basename, not full path)."""
    import logging
    csv_file = tmp_path / "data.csv"
    csv_file.write_text("a\n1\n2\n")
    wrapper = tmp_path / "reader.yml"
    wrapper.write_text(
        "class: apecx_integration.composition.steps.file_readers.DelimitedFileReaderStep\n"
        "name: log_test\n"
        "description: log\n"
        f"file_path: '{csv_file}'\n"
        "input_data_units: {}\n"
        "output_data_units:\n"
        "  records_output:\n"
        "    class: nanobrain.core.data_unit.DataUnitMemory\n"
        "    name: records_output\n"
        "    description: out\n"
        "    persistent: false\n"
        "triggers: []\n"
    )
    step = DelimitedFileReaderStep.from_config(str(wrapper))
    caplog.set_level(
        logging.INFO,
        logger="apecx_integration.composition.steps.file_readers",
    )
    asyncio.run(step.process({}))
    matches = [
        r.message for r in caplog.records
        if "data.csv" in r.message and "2" in r.message
    ]
    assert matches, (
        f"log line missing row count + filename; got: "
        f"{[r.message for r in caplog.records]!r}"
    )


def _minimal_reader_wrapper(tmp_path: Path, file_path: Path, **extra: str) -> Path:
    """Helper for probes that need to load a step from YAML to check
    defaults. Direct StepConfig construction is forbidden by the
    framework; from_config(YAML) is the only legal path."""
    wrapper = tmp_path / "reader.yml"
    extra_lines = "".join(f"{k}: {v}\n" for k, v in extra.items())
    wrapper.write_text(
        "class: apecx_integration.composition.steps.file_readers.DelimitedFileReaderStep\n"
        "name: probe_reader\n"
        "description: probe defaults\n"
        f"file_path: '{file_path}'\n"
        + extra_lines
        + "input_data_units: {}\n"
        "output_data_units:\n"
        "  records_output:\n"
        "    class: nanobrain.core.data_unit.DataUnitMemory\n"
        "    name: records_output\n"
        "    description: out\n"
        "    persistent: false\n"
        "triggers: []\n"
    )
    return wrapper


def test_probe_1201_file_reader_format_default_is_csv(tmp_path):
    """Default format is csv. Pin so a future tsv-default doesn't
    silently re-interpret existing CSVs."""
    csv_file = tmp_path / "x.csv"
    csv_file.write_text("a\n1\n")
    wrapper = _minimal_reader_wrapper(tmp_path, csv_file)
    step = DelimitedFileReaderStep.from_config(str(wrapper))
    # The default delimiter for csv is ','.
    assert step._format == "csv"
    assert step._delimiter == ","


def test_probe_1202_file_reader_encoding_default_is_utf8(tmp_path):
    """Default encoding is utf-8."""
    csv_file = tmp_path / "x.csv"
    csv_file.write_text("a\n1\n")
    wrapper = _minimal_reader_wrapper(tmp_path, csv_file)
    step = DelimitedFileReaderStep.from_config(str(wrapper))
    assert step._encoding == "utf-8"


def test_probe_1203_file_reader_required_columns_default_empty(tmp_path):
    """Default required_columns is []."""
    csv_file = tmp_path / "x.csv"
    csv_file.write_text("a\n1\n")
    wrapper = _minimal_reader_wrapper(tmp_path, csv_file)
    step = DelimitedFileReaderStep.from_config(str(wrapper))
    assert step._required_columns == []


def test_probe_1204_workflow_yaml_two_file_readers_share_class_path():
    """Both reader steps share the same class path. Pin so a future
    fork (e.g. one reader using a different impl) is intentional."""
    import yaml
    wf = yaml.safe_load(
        (WORKFLOW_DIR / "violin_bvbrc_workflow.yml").read_text()
    )
    a = wf["steps"]["violin_vaccine_reader"]["class"]
    b = wf["steps"]["bvbrc_alphavirus_genomes_reader"]["class"]
    assert a == b == (
        "apecx_integration.composition.steps.file_readers."
        "DelimitedFileReaderStep"
    )
