"""Probe batch 23 — parsers against REAL data files (probes 605-629).

Cluster AQ (batch 22) was a parser that silently skipped 100% of
real BV-BRC FASTA headers because its expected ``[md5=<hex>]``
format never matched the actual pipe-delimited shape. Tests didn't
catch it because no test exercised the parser against real data.

This batch hunts the same bug class systematically: every parser
that reads files in ``data/`` is run against the real files in this
repository and the result is asserted to be non-empty and shaped
correctly. If the real-data file moves, schema-drifts, or a future
parser change breaks compatibility, these probes flip red — exactly
the failure mode cluster AQ taught us to defend against.

Probes that need real data files skip if the file is missing
(allows CI runs without the snapshot to still pass; the local
developer workflow always has them).
"""

from __future__ import annotations

import asyncio
import csv
import os
from pathlib import Path

import pytest


pytestmark = pytest.mark.integration


# Resolve the repository's data directory. The tests live in
# apecx-mcp-integration/tests/integration; data/ is at the workspace
# root (one level up).
_WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
_BVBRC_DIR = _WORKSPACE_ROOT / "data" / "bvbrc_cache"
_VIOLIN_DIR = _WORKSPACE_ROOT / "data" / "violin"


def _require_file(path: Path) -> Path:
    if not path.is_file():
        pytest.skip(f"real data file not present: {path}")
    return path


def _require_dir(path: Path) -> Path:
    if not path.is_dir():
        pytest.skip(f"real data directory not present: {path}")
    return path


def _make_snapshot_tool():
    """Bypass nanobrain's from_config check to instantiate the tool
    for direct method exercise. We're calling the parsing methods
    only, which don't depend on the framework lifecycle."""
    from apecx_integration.composition.tools.bv_brc_snapshot_tool import (
        BVBRCSnapshotTool,
    )
    return BVBRCSnapshotTool.__new__(BVBRCSnapshotTool)


# ---------------------------------------------------------------------------
# BV-BRC snapshot against real data — probes 605-614
# ---------------------------------------------------------------------------


def test_probe_605_real_alphavirus_genomes_tsv_loads() -> None:
    """The real ``alphavirus_genomes.tsv`` must parse and produce
    GenomeData rows. This is the smoke probe that would have
    caught cluster AQ if applied to TSV instead of FASTA."""
    p = _require_file(_BVBRC_DIR / "alphavirus_genomes.tsv")
    os.environ["APECX_BVBRC_SNAPSHOT_DIR"] = str(_BVBRC_DIR)
    try:
        tool = _make_snapshot_tool()
        genomes = asyncio.run(tool.download_alphavirus_genomes())
        assert len(genomes) > 100, (
            f"PROBE 605: expected hundreds of genomes from real "
            f"alphavirus snapshot; got {len(genomes)}"
        )
        # Sanity: every row has a non-empty id + name
        assert all(g.genome_id for g in genomes[:10])
        assert all(g.genome_name for g in genomes[:10])
    finally:
        os.environ.pop("APECX_BVBRC_SNAPSHOT_DIR", None)


def test_probe_606_alphavirus_genomes_limit_truncates() -> None:
    p = _require_file(_BVBRC_DIR / "alphavirus_genomes.tsv")
    os.environ["APECX_BVBRC_SNAPSHOT_DIR"] = str(_BVBRC_DIR)
    try:
        tool = _make_snapshot_tool()
        genomes = asyncio.run(tool.download_alphavirus_genomes(limit=5))
        assert len(genomes) == 5
    finally:
        os.environ.pop("APECX_BVBRC_SNAPSHOT_DIR", None)


def test_probe_607_get_unique_protein_md5s_dedups() -> None:
    """The real proteins.tsv has multiple rows per md5 (different
    feature_id mapping to same sequence). Dedup by md5 must
    collapse these — otherwise downstream code does redundant
    sequence lookups."""
    _require_file(_BVBRC_DIR / "alphavirus_proteins.tsv")
    os.environ["APECX_BVBRC_SNAPSHOT_DIR"] = str(_BVBRC_DIR)
    try:
        tool = _make_snapshot_tool()
        # Pick a few real genome ids
        genomes = asyncio.run(tool.download_alphavirus_genomes(limit=3))
        proteins = asyncio.run(
            tool.get_unique_protein_md5s([g.genome_id for g in genomes])
        )
        md5s = [p.aa_sequence_md5 for p in proteins]
        assert len(md5s) == len(set(md5s)), (
            "PROBE 607: get_unique_protein_md5s did not dedup by md5"
        )
    finally:
        os.environ.pop("APECX_BVBRC_SNAPSHOT_DIR", None)


def test_probe_608_get_unique_protein_md5s_filters_by_genome() -> None:
    """When given a small set of genome_ids, the result must NOT
    include proteins from other genomes — otherwise the snapshot
    tool returns more data than the caller asked for."""
    _require_file(_BVBRC_DIR / "alphavirus_proteins.tsv")
    os.environ["APECX_BVBRC_SNAPSHOT_DIR"] = str(_BVBRC_DIR)
    try:
        tool = _make_snapshot_tool()
        genomes = asyncio.run(tool.download_alphavirus_genomes(limit=2))
        target_ids = {g.genome_id for g in genomes}
        proteins = asyncio.run(
            tool.get_unique_protein_md5s(list(target_ids))
        )
        # Every protein's genome_id (or genome_id field if present)
        # must be in the target set.
        seen_genome_ids = {p.genome_id for p in proteins if p.genome_id}
        assert seen_genome_ids <= target_ids, (
            f"PROBE 608: protein returned for non-requested genome: "
            f"{seen_genome_ids - target_ids}"
        )
    finally:
        os.environ.pop("APECX_BVBRC_SNAPSHOT_DIR", None)


def test_probe_609_real_alphavirus_fasta_extracts_all_sequences() -> None:
    """Cluster AQ regression — pre-fix, this was 0/18632.
    Post-fix, every header must yield a sequence."""
    p = _require_file(_BVBRC_DIR / "alphavirus_proteins_annotated.fasta")
    from apecx_integration.composition.tools.bv_brc_snapshot_tool import (
        _read_fasta_by_md5,
    )
    seqs = _read_fasta_by_md5(p)
    header_count = sum(1 for line in p.read_text().splitlines() if line.startswith(">"))
    assert header_count > 0
    extraction_rate = len(seqs) / header_count
    assert extraction_rate >= 0.99, (
        f"PROBE 609: parser only extracts {extraction_rate:.1%} of real "
        f"FASTA headers (expected ≥99%). Cluster AQ regression: "
        f"the parser may have lost real-format compatibility again."
    )


def test_probe_610_alphavirus_genomes_tsv_column_set() -> None:
    """Lock the column-set the snapshot expects. A schema drift
    that adds / renames columns must surface here, not at workflow
    runtime."""
    p = _require_file(_BVBRC_DIR / "alphavirus_genomes.tsv")
    with p.open(encoding="utf-8") as f:
        header = next(csv.reader(f, delimiter="\t"))
    expected = {"genome.genome_id", "genome.genome_name"}
    actual = set(header)
    assert expected <= actual, (
        f"PROBE 610: alphavirus_genomes.tsv missing required cols: {expected - actual}"
    )


def test_probe_611_alphavirus_proteins_tsv_column_set() -> None:
    p = _require_file(_BVBRC_DIR / "alphavirus_proteins.tsv")
    with p.open(encoding="utf-8") as f:
        header = next(csv.reader(f, delimiter="\t"))
    expected = {
        "genome.genome_id",
        "feature.aa_sequence_md5",
    }
    actual = set(header)
    assert expected <= actual, (
        f"PROBE 611: alphavirus_proteins.tsv missing required cols: {expected - actual}"
    )


def test_probe_612_alphavirus_fasta_header_count_marker() -> None:
    """Lock the expected header count so a snapshot-replacement
    that silently swaps in an empty / different file surfaces here.
    The real alphavirus_proteins_annotated.fasta has 18,632 headers
    as of 2026-04-26."""
    p = _require_file(_BVBRC_DIR / "alphavirus_proteins_annotated.fasta")
    count = sum(1 for line in p.read_text().splitlines() if line.startswith(">"))
    assert count >= 10000, (
        f"PROBE 612: alphavirus_proteins_annotated.fasta has {count} "
        f"headers (expected ≥10,000 — snapshot may be truncated)."
    )


def test_probe_613_bvbrc_snapshot_tool_blocks_shell_execute() -> None:
    """BVBRCSnapshotTool must refuse execute_command calls. A
    snapshot-mode deployment has no BV-BRC CLI; falling through to
    the base class would produce cryptic 'p3-all-genomes not found'
    errors deep in the workflow."""
    tool = _make_snapshot_tool()
    with pytest.raises(RuntimeError, match="snapshot-only"):
        asyncio.run(tool.execute_command(["p3-all-genomes"]))


def test_probe_614_filter_genomes_passthrough_when_length_zero() -> None:
    """Snapshot 2-column TSV has no genome_length, so all loaded
    GenomeData rows have length=0. filter_genomes_by_size must
    pass through (NOT drop everything as 'too small')."""
    from nanobrain.library.tools.bioinformatics.bv_brc_tool import GenomeData
    tool = _make_snapshot_tool()
    genomes = [
        GenomeData(genome_id="x", genome_length=0, genome_name="x",
                   taxon_lineage="", genome_status="snapshot", contigs=0),
        GenomeData(genome_id="y", genome_length=0, genome_name="y",
                   taxon_lineage="", genome_status="snapshot", contigs=0),
    ]
    out = asyncio.run(tool.filter_genomes_by_size(genomes))
    assert len(out) == 2, "filter must pass-through when length=0"


# ---------------------------------------------------------------------------
# VIOLIN CSV smoke probes — probes 615-619
# ---------------------------------------------------------------------------


def test_probe_615_violin_vaccine_csv_loads() -> None:
    p = _require_file(_VIOLIN_DIR / "Vaccine_Information.csv")
    with p.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) > 100
    assert "Vaccine" in rows[0] or "Vaccine_Name" in rows[0]


def test_probe_616_violin_pathogen_csv_loads() -> None:
    p = _require_file(_VIOLIN_DIR / "Pathogen_Information.csv")
    with p.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) > 50


def test_probe_617_violin_gene_csv_loads() -> None:
    p = _require_file(_VIOLIN_DIR / "Gene_Information.csv")
    with p.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) > 0


def test_probe_618_violin_csvs_are_utf8() -> None:
    """Every VIOLIN CSV in data/violin/ must be readable as UTF-8.
    Latin-1 bytes that decode under utf-8 produce mojibake — the
    DelimitedFileReaderStep would silently load wrong values."""
    d = _require_dir(_VIOLIN_DIR)
    csv_files = list(d.glob("*.csv"))
    assert csv_files, "no .csv files in VIOLIN directory"
    for csv_path in csv_files:
        try:
            csv_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            pytest.fail(
                f"PROBE 618: VIOLIN CSV {csv_path.name} is not UTF-8 "
                f"(decode error: {e}). DelimitedFileReaderStep would "
                f"silently misread it."
            )


def test_probe_619_vaccine_csv_has_unique_ids() -> None:
    """The 'id' column in Vaccine_Information.csv must be unique.
    Duplicate ids would silently merge rows downstream — a shape
    invariant that's load-bearing for VIOLIN-keyed lookups."""
    p = _require_file(_VIOLIN_DIR / "Vaccine_Information.csv")
    with p.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if "id" in rows[0]:
        ids = [r["id"] for r in rows if r.get("id")]
        assert len(ids) == len(set(ids)), (
            "PROBE 619: duplicate ids in Vaccine_Information.csv"
        )


# ---------------------------------------------------------------------------
# Snapshot directory invariants — probes 620-624
# ---------------------------------------------------------------------------


def test_probe_620_violin_dir_present() -> None:
    """The VIOLIN directory must exist OR be skip-tagged. We
    enforce the 'or skip' contract — the alternative (silently
    missing data) is exactly the bug class."""
    d = _VIOLIN_DIR
    if not d.is_dir():
        pytest.skip("VIOLIN data directory absent")
    assert (d / "Vaccine_Information.csv").is_file()
    assert (d / "Pathogen_Information.csv").is_file()


def test_probe_621_bvbrc_dir_has_required_files() -> None:
    d = _require_dir(_BVBRC_DIR)
    expected = {
        "alphavirus_genomes.tsv",
        "alphavirus_proteins.tsv",
        "alphavirus_proteins_annotated.fasta",
    }
    actual = {p.name for p in d.iterdir() if p.is_file()}
    missing = expected - actual
    assert not missing, f"PROBE 621: BV-BRC dir missing: {missing}"


def test_probe_622_alphavirus_filtered_proteins_tsv_parses() -> None:
    """The .filtered.tsv subset must have the same column shape
    as the full proteins.tsv — otherwise downstream code that
    falls back to the filtered file silently sees wrong columns."""
    p = _BVBRC_DIR / "alphavirus_proteins.filtered.tsv"
    if not p.is_file():
        pytest.skip("filtered TSV absent")
    with p.open(encoding="utf-8") as f:
        header = next(csv.reader(f, delimiter="\t"))
    expected = {"genome.genome_id", "feature.aa_sequence_md5"}
    actual = set(header)
    assert expected <= actual, (
        f"PROBE 622: filtered.tsv schema differs from canonical: "
        f"missing {expected - actual}"
    )


def test_probe_623_chikungunya_proteins_tsv_parses_when_present() -> None:
    """Same column-shape check for the chikungunya snapshot."""
    p = _BVBRC_DIR / "chikungunya_virus_proteins.tsv"
    if not p.is_file():
        pytest.skip("chikungunya proteins TSV absent")
    with p.open(encoding="utf-8") as f:
        header = next(csv.reader(f, delimiter="\t"))
    expected = {"genome.genome_id", "feature.aa_sequence_md5"}
    actual = set(header)
    assert expected <= actual


def test_probe_624_chikungunya_genomes_tsv_parses_when_present() -> None:
    p = _BVBRC_DIR / "chikungunya_virus_genomes.tsv"
    if not p.is_file():
        pytest.skip("chikungunya genomes TSV absent")
    from apecx_integration.composition.tools.bv_brc_snapshot_tool import (
        _read_tsv,
    )
    rows = _read_tsv(p)
    assert len(rows) > 0
    assert "genome.genome_id" in rows[0]


# ---------------------------------------------------------------------------
# Cross-file invariants — probes 625-629
# ---------------------------------------------------------------------------


def test_probe_625_protein_md5s_have_sequences() -> None:
    """Every md5 referenced by alphavirus_proteins.tsv must have a
    sequence in alphavirus_proteins_annotated.fasta. A snapshot that
    drifts these out of sync would produce missing-sequence
    warnings on every workflow run."""
    tsv_p = _require_file(_BVBRC_DIR / "alphavirus_proteins.tsv")
    fasta_p = _require_file(_BVBRC_DIR / "alphavirus_proteins_annotated.fasta")
    from apecx_integration.composition.tools.bv_brc_snapshot_tool import (
        _read_tsv, _read_fasta_by_md5,
    )
    proteins = _read_tsv(tsv_p)
    md5s_referenced = {r["feature.aa_sequence_md5"] for r in proteins
                       if r.get("feature.aa_sequence_md5")}
    sequences_available = set(_read_fasta_by_md5(fasta_p).keys())
    # Allow a small mismatch (e.g. md5s with no FASTA entry — rare
    # but documented in get_feature_sequences). We require ≥95%
    # coverage so a structural drift surfaces but historical
    # imperfections don't blow up.
    if not md5s_referenced:
        pytest.skip("no md5s in proteins.tsv to cross-check")
    coverage = len(md5s_referenced & sequences_available) / len(md5s_referenced)
    assert coverage >= 0.95, (
        f"PROBE 625: only {coverage:.1%} of TSV md5s have FASTA "
        f"sequences — snapshot is out of sync."
    )


def test_probe_626_protein_genomes_subset_of_genomes_tsv() -> None:
    """Every genome_id referenced in proteins.tsv must exist in
    genomes.tsv. Otherwise a snapshot has dangling protein rows."""
    g_p = _require_file(_BVBRC_DIR / "alphavirus_genomes.tsv")
    p_p = _require_file(_BVBRC_DIR / "alphavirus_proteins.tsv")
    from apecx_integration.composition.tools.bv_brc_snapshot_tool import (
        _read_tsv,
    )
    genomes = _read_tsv(g_p)
    proteins = _read_tsv(p_p)
    genome_ids = {r["genome.genome_id"] for r in genomes
                  if r.get("genome.genome_id")}
    protein_genome_ids = {r["genome.genome_id"] for r in proteins
                          if r.get("genome.genome_id")}
    dangling = protein_genome_ids - genome_ids
    # Allow a few — historical snapshots may have minor drift.
    if protein_genome_ids:
        dangling_rate = len(dangling) / len(protein_genome_ids)
        assert dangling_rate < 0.05, (
            f"PROBE 626: {dangling_rate:.1%} of proteins reference "
            f"genome_ids not in genomes.tsv — snapshot mismatch."
        )


def test_probe_627_snapshot_dir_env_override_actually_redirects(tmp_path) -> None:
    """If APECX_BVBRC_SNAPSHOT_DIR is set, the resolver must use
    THAT path, not the default. Probe 580 (batch 22) confirmed
    the resolver's return value; this one confirms the snapshot
    tool uses the resolver consistently."""
    custom = tmp_path / "custom"
    custom.mkdir()
    (custom / "alphavirus_genomes.tsv").write_text(
        "genome.genome_id\tgenome.genome_name\n"
        "TEST_ID\tTest Genome\n",
        encoding="utf-8",
    )
    os.environ["APECX_BVBRC_SNAPSHOT_DIR"] = str(custom)
    try:
        tool = _make_snapshot_tool()
        genomes = asyncio.run(tool.download_alphavirus_genomes())
        assert len(genomes) == 1
        assert genomes[0].genome_id == "TEST_ID"
    finally:
        os.environ.pop("APECX_BVBRC_SNAPSHOT_DIR", None)


def test_probe_628_chikungunya_fasta_format_consistency() -> None:
    """If the chikungunya FASTA has content, it must use the same
    format as alphavirus (so the same parser works). An empty file
    is acceptable (placeholder). A non-empty file with NO valid
    headers is a red flag — same shape as the cluster AQ bug."""
    p = _BVBRC_DIR / "chikungunya_virus_proteins_annotated.fasta"
    if not p.is_file():
        pytest.skip("chikungunya FASTA absent")
    text = p.read_text()
    if not text.strip():
        pytest.skip("chikungunya FASTA is empty placeholder")
    from apecx_integration.composition.tools.bv_brc_snapshot_tool import (
        _read_fasta_by_md5,
    )
    seqs = _read_fasta_by_md5(p)
    header_count = sum(1 for line in text.splitlines() if line.startswith(">"))
    if header_count > 0:
        assert len(seqs) >= header_count * 0.95, (
            f"PROBE 628: chikungunya FASTA parser extraction rate "
            f"{len(seqs)}/{header_count} — possible format drift "
            f"(same class as cluster AQ)."
        )


def test_probe_629_extract_md5_handles_real_alphavirus_header() -> None:
    """Direct unit-style probe of the new helper against an exact
    string from the production alphavirus FASTA (sampled
    2026-04-26). Locks the parser-vs-real-data contract for cluster
    AQ at the lowest level."""
    from apecx_integration.composition.tools.bv_brc_snapshot_tool import (
        _extract_md5_from_header,
    )
    real_header = (
        ">fig_37124.7183.mat_peptide.2|protease nsp2|"
        "unknown|75e21b1d49191c5e97f681fe38e3f274"
    )
    md5 = _extract_md5_from_header(real_header)
    assert md5 == "75e21b1d49191c5e97f681fe38e3f274"
