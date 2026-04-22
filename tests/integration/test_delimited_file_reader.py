"""T02 Phase 3 integration: DelimitedFileReaderStep against real
VIOLIN + BV-BRC snapshot data.

No mocks. Uses ``data/violin/*.csv`` and ``data/bvbrc_cache/*.tsv``
from the workspace (absolute paths so the tests work regardless of
pytest's rootdir). Proves the reader actually handles the real
schemas without needing a synthetic fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from apecx_integration.composition.steps.file_readers import DelimitedFileReaderStep

pytestmark = pytest.mark.integration

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = WORKSPACE_ROOT / "data"
VIOLIN_VACCINE_CSV = DATA_DIR / "violin" / "Vaccine_Information.csv"
BVBRC_GENOMES_TSV = DATA_DIR / "bvbrc_cache" / "alphavirus_genomes.tsv"
BVBRC_PROTEINS_TSV = DATA_DIR / "bvbrc_cache" / "alphavirus_proteins.tsv"

SKIP_REASON_NO_DATA = (
    "Real snapshot data not available under data/. Set up data/violin/ and "
    "data/bvbrc_cache/ snapshots to activate this test."
)


def _make_step(tmp_path, **config_overrides) -> DelimitedFileReaderStep:
    config = {
        "name": "test_reader",
        "description": "test",
        "file_path": str(VIOLIN_VACCINE_CSV),
        "format": "csv",
    }
    config.update(config_overrides)
    config_path = tmp_path / "reader_config.yml"
    config_path.write_text(yaml.safe_dump(config))
    return DelimitedFileReaderStep.from_config(str(config_path))


@pytest.mark.skipif(not VIOLIN_VACCINE_CSV.is_file(), reason=SKIP_REASON_NO_DATA)
async def test_reads_violin_vaccine_csv_real_data(tmp_path) -> None:
    step = _make_step(
        tmp_path,
        file_path=str(VIOLIN_VACCINE_CSV),
        delimiter=",",
        required_columns=["id", "Vaccine", "Vaccine_Ontology_ID"],
    )
    result = await step.process({})
    assert result["row_count"] > 100  # VIOLIN has ~5k vaccines
    assert result["source_path"] == str(VIOLIN_VACCINE_CSV.resolve())
    assert isinstance(result["records"], list)
    first = result["records"][0]
    assert "id" in first
    assert "Vaccine" in first
    assert "Vaccine_Ontology_ID" in first


@pytest.mark.skipif(not BVBRC_GENOMES_TSV.is_file(), reason=SKIP_REASON_NO_DATA)
async def test_reads_bvbrc_genomes_tsv_real_data(tmp_path) -> None:
    step = _make_step(
        tmp_path,
        file_path=str(BVBRC_GENOMES_TSV),
        format="tsv",
        required_columns=["genome.genome_id", "genome.genome_name"],
    )
    result = await step.process({})
    assert result["row_count"] >= 1
    first = result["records"][0]
    assert "genome.genome_id" in first
    assert "genome.genome_name" in first


@pytest.mark.skipif(not BVBRC_PROTEINS_TSV.is_file(), reason=SKIP_REASON_NO_DATA)
async def test_reads_bvbrc_proteins_tsv_real_data(tmp_path) -> None:
    """Uses a TSV with more columns than the genomes file to confirm
    the reader isn't hardcoded to a specific column count.
    """
    step = _make_step(
        tmp_path,
        file_path=str(BVBRC_PROTEINS_TSV),
        format="tsv",
        required_columns=[
            "genome.genome_id",
            "feature.patric_id",
            "feature.product",
            "feature.aa_sequence_md5",
        ],
    )
    result = await step.process({})
    assert result["row_count"] >= 1


async def test_missing_required_column_fails_loudly(tmp_path) -> None:
    """Required-columns guard must fire at process() time (the real
    file exists; the column we ask for doesn't). Fail-loud matters
    here: the downstream joiner would silently mis-align rows
    otherwise.
    """
    if not VIOLIN_VACCINE_CSV.is_file():
        pytest.skip(SKIP_REASON_NO_DATA)
    step = _make_step(
        tmp_path,
        file_path=str(VIOLIN_VACCINE_CSV),
        required_columns=["this_column_does_not_exist"],
    )
    with pytest.raises(ValueError, match="missing required columns"):
        await step.process({})


async def test_missing_file_fails_with_resolved_path(tmp_path) -> None:
    step = _make_step(tmp_path, file_path="/nonexistent/file.csv")
    with pytest.raises(FileNotFoundError, match="/nonexistent/file.csv"):
        await step.process({})


async def test_unknown_format_rejected_at_init(tmp_path) -> None:
    """Fail-loud guard: ``format: psv`` (typo / unsupported) is caught
    at step init by the Pydantic Literal, not at file-read time.
    """
    with pytest.raises(Exception):  # noqa: B017 — pydantic-version-dependent exc class
        _make_step(tmp_path, format="psv")


@pytest.mark.skipif(not VIOLIN_VACCINE_CSV.is_file(), reason=SKIP_REASON_NO_DATA)
async def test_no_required_columns_still_works(tmp_path) -> None:
    """Required-columns default is []; the reader should accept any
    schema when no guard is specified.
    """
    step = _make_step(tmp_path, file_path=str(VIOLIN_VACCINE_CSV))
    result = await step.process({})
    assert result["row_count"] > 0


async def test_wrapper_yamls_load_via_from_config() -> None:
    """The two shipped wrapper YAMLs
    (violin_vaccine_reader.yml + bvbrc_alphavirus_genomes_reader.yml)
    round-trip through from_config without a tmp_path. Cheap
    correctness gate matching test_workflow_wrapper_yamls_load.py's
    pattern from T02 Phase 2.
    """
    steps_dir = (
        Path(__file__).resolve().parents[1].parent
        / "src"
        / "apecx_integration"
        / "composition"
        / "workflows"
        / "violin_bvbrc"
        / "steps"
    )
    violin = DelimitedFileReaderStep.from_config(
        str(steps_dir / "violin_vaccine_reader.yml")
    )
    assert violin.name == "violin_vaccine_reader"
    assert violin._format == "csv"
    assert violin._delimiter == ","

    bvbrc = DelimitedFileReaderStep.from_config(
        str(steps_dir / "bvbrc_alphavirus_genomes_reader.yml")
    )
    assert bvbrc.name == "bvbrc_alphavirus_genomes_reader"
    assert bvbrc._format == "tsv"
    assert bvbrc._delimiter == "\t"
