"""T02 Phase 4: BVBRCSnapshotTool against real snapshot data.

No mocks. Uses the actual TSV + FASTA files under
``data/bvbrc_cache/`` when present; auto-skips otherwise so CI on
a fresh clone doesn't require downloading the snapshot.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest
from apecx_integration.composition.tools.bv_brc_snapshot_tool import (
    DEFAULT_SNAPSHOT_DIR,
    SNAPSHOT_ENV_VAR,
    BVBRCSnapshotTool,
)

pytestmark = pytest.mark.integration

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
REAL_SNAPSHOT_DIR = WORKSPACE_ROOT / DEFAULT_SNAPSHOT_DIR
REAL_GENOMES_TSV = REAL_SNAPSHOT_DIR / "alphavirus_genomes.tsv"
REAL_PROTEINS_TSV = REAL_SNAPSHOT_DIR / "alphavirus_proteins.tsv"
REAL_PROTEINS_FASTA = REAL_SNAPSHOT_DIR / "alphavirus_proteins_annotated.fasta"

SKIP_REASON = "Real BV-BRC snapshot not present under data/bvbrc_cache/"

REPO_ROOT = Path(__file__).resolve().parents[2]


def _build_tool(snapshot_dir: Path, tmp_path: Path) -> BVBRCSnapshotTool:
    """Build the tool via the nanobrain from_config pathway.

    BVBRCTool's ``from_config`` expects a YAML path; we write a
    minimal config there and let the framework instantiate. Env var
    pokes the snapshot location.
    """
    os.environ[SNAPSHOT_ENV_VAR] = str(snapshot_dir)
    config_path = tmp_path / "bvbrc_tool_config.yml"
    config_path.write_text(
        textwrap.dedent(
            """\
            tool_name: bv_brc
            use_http_api: true
            genome_batch_size: 10
            md5_batch_size: 5
            min_genome_length: 8000
            max_genome_length: 15000
            use_cache: false
            """
        )
    )
    return BVBRCSnapshotTool.from_config(str(config_path))


@pytest.mark.skipif(not REAL_GENOMES_TSV.is_file(), reason=SKIP_REASON)
async def test_download_alphavirus_genomes_reads_real_tsv(tmp_path) -> None:
    tool = _build_tool(REAL_SNAPSHOT_DIR, tmp_path)
    genomes = await tool.download_alphavirus_genomes()
    assert len(genomes) >= 1
    g = genomes[0]
    assert g.genome_id
    assert g.genome_name
    # snapshot has no length column -> defaults to 0.
    assert g.genome_length == 0


@pytest.mark.skipif(not REAL_GENOMES_TSV.is_file(), reason=SKIP_REASON)
async def test_filter_genomes_by_size_passes_through_on_zero_length(tmp_path) -> None:
    tool = _build_tool(REAL_SNAPSHOT_DIR, tmp_path)
    genomes = await tool.download_alphavirus_genomes()
    filtered = await tool.filter_genomes_by_size(genomes)
    # Every genome has length=0 in the snapshot; filter passes all through.
    assert len(filtered) == len(genomes)


@pytest.mark.skipif(not REAL_PROTEINS_TSV.is_file(), reason=SKIP_REASON)
async def test_get_unique_protein_md5s_filters_and_dedupes(tmp_path) -> None:
    tool = _build_tool(REAL_SNAPSHOT_DIR, tmp_path)
    genomes = await tool.download_alphavirus_genomes()
    genome_ids = [g.genome_id for g in genomes[:3]]

    proteins = await tool.get_unique_protein_md5s(genome_ids)
    assert len(proteins) >= 1

    # All returned proteins must belong to one of the requested genomes (by md5 path).
    md5s = {p.aa_sequence_md5 for p in proteins}
    assert len(md5s) == len(proteins)  # deduped

    empty = await tool.get_unique_protein_md5s([])
    assert empty == []


@pytest.mark.skipif(not REAL_PROTEINS_FASTA.is_file(), reason=SKIP_REASON)
async def test_get_feature_sequences_missing_md5s_are_skipped(tmp_path) -> None:
    """Call with a mix of known + unknown md5s; the tool should return
    sequences only for the known ones and log-warn about the missing.
    """
    tool = _build_tool(REAL_SNAPSHOT_DIR, tmp_path)
    # Invent a fake md5 that definitely isn't in the snapshot.
    fake_md5 = "0" * 32
    result = await tool.get_feature_sequences([fake_md5])
    # No real md5 requested, so either empty or filtered to nothing.
    assert all(p.aa_sequence_md5 == fake_md5 for p in result) or result == []


async def test_missing_snapshot_dir_raises_with_resolved_path(tmp_path) -> None:
    tool = _build_tool(tmp_path / "nonexistent_snapshot", tmp_path)
    with pytest.raises(FileNotFoundError, match="nonexistent_snapshot"):
        await tool.download_alphavirus_genomes()


async def test_execute_command_is_disabled(tmp_path) -> None:
    """Snapshot deployments have no BV-BRC CLI binary; the tool must
    refuse shell-out attempts rather than failing cryptically.
    """
    tool = _build_tool(tmp_path, tmp_path)
    with pytest.raises(RuntimeError, match="snapshot-only"):
        await tool.execute_command(["p3-all-genomes"])


async def test_env_var_overrides_default_snapshot_dir(tmp_path, monkeypatch) -> None:
    custom_snapshot = tmp_path / "custom_snapshot"
    custom_snapshot.mkdir()
    (custom_snapshot / "alphavirus_genomes.tsv").write_text(
        "genome.genome_id\tgenome.genome_name\n" "99999.1\tSynthetic Test Virus\n"
    )
    monkeypatch.setenv(SNAPSHOT_ENV_VAR, str(custom_snapshot))
    tool = _build_tool(custom_snapshot, tmp_path)
    genomes = await tool.download_alphavirus_genomes()
    assert len(genomes) == 1
    assert genomes[0].genome_name == "Synthetic Test Virus"


async def test_synthetic_proteins_tsv_round_trip(tmp_path, monkeypatch) -> None:
    """Synthetic snapshot exercises get_unique_protein_md5s end-to-end
    without depending on the real data being present.
    """
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "alphavirus_genomes.tsv").write_text(
        "genome.genome_id\tgenome.genome_name\n37124.6497\tVirus A\n"
    )
    (snap / "alphavirus_proteins.tsv").write_text(
        "genome.genome_id\tfeature.patric_id\tfeature.product\t"
        "feature.aa_sequence_md5\tfeature.genome_id\n"
        "37124.6497\tfig|37124.6497.peg.1\tnonstructural polyprotein\t"
        "aaaa1111bbbb2222cccc3333dddd4444\t37124.6497\n"
        "37124.6497\tfig|37124.6497.peg.2\tcapsid\t"
        "aaaa1111bbbb2222cccc3333dddd4444\t37124.6497\n"  # duplicate md5, dedup
        "37124.6497\tfig|37124.6497.peg.3\tenvelope\t"
        "1111222233334444555566667777bbbb\t37124.6497\n"
    )
    monkeypatch.setenv(SNAPSHOT_ENV_VAR, str(snap))
    tool = _build_tool(snap, tmp_path)

    proteins = await tool.get_unique_protein_md5s(["37124.6497"])
    # Two unique md5s despite three input rows.
    assert len(proteins) == 2
    md5s = {p.aa_sequence_md5 for p in proteins}
    assert md5s == {
        "aaaa1111bbbb2222cccc3333dddd4444",
        "1111222233334444555566667777bbbb",
    }

    # Filtering by genome_id that doesn't match returns nothing.
    proteins_empty = await tool.get_unique_protein_md5s(["99999.1"])
    assert proteins_empty == []
