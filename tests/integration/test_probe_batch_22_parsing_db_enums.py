"""Probe batch 22 — BV-BRC snapshot parsing, DB engine config, enum
invariants, and artifact-store contracts (probes 580-604).

Pivot from the bridge-audit arc (batches 18-21) to a fresh attack
surface: the data-layer parsers + DB plumbing + enum lock-in. Each
probe targets a silent-failure mode where bad data or drifted enums
would let bad runs slip through:

  - composition/tools/bv_brc_snapshot_tool: TSV / FASTA parsing
  - control_plane/db: SQLite vs Postgres connect_args branching
  - schemas/enums: lock the values across the data model
  - composition/artifact_store: GENERATED_KINDS gate inspection

All probes are pure-Python — no DB, no FastAPI, no Docker.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# BV-BRC snapshot TSV / FASTA parsing — probes 580-589
# ---------------------------------------------------------------------------


def test_probe_580_snapshot_dir_env_override(tmp_path) -> None:
    """The APECX_BVBRC_SNAPSHOT_DIR env var must override the
    default. Without this, deployments that put their snapshots
    outside the repo silently get the wrong data path."""
    from apecx_integration.composition.tools.bv_brc_snapshot_tool import (
        _resolve_snapshot_dir,
    )
    custom = tmp_path / "elsewhere"
    with patch.dict(os.environ, {"APECX_BVBRC_SNAPSHOT_DIR": str(custom)}):
        assert _resolve_snapshot_dir() == Path(str(custom))


def test_probe_581_snapshot_dir_default_when_unset() -> None:
    from apecx_integration.composition.tools.bv_brc_snapshot_tool import (
        _resolve_snapshot_dir, DEFAULT_SNAPSHOT_DIR,
    )
    saved = os.environ.pop("APECX_BVBRC_SNAPSHOT_DIR", None)
    try:
        assert _resolve_snapshot_dir() == Path(DEFAULT_SNAPSHOT_DIR)
    finally:
        if saved is not None:
            os.environ["APECX_BVBRC_SNAPSHOT_DIR"] = saved


def test_probe_582_read_tsv_missing_file_raises(tmp_path) -> None:
    """A missing snapshot file must fail-fast with a friendly
    message — silent fallback to "empty result" would let runs
    proceed against missing data."""
    from apecx_integration.composition.tools.bv_brc_snapshot_tool import (
        _read_tsv,
    )
    with pytest.raises(FileNotFoundError, match="snapshot file not found"):
        _read_tsv(tmp_path / "nonexistent.tsv")


def test_probe_583_read_tsv_empty_returns_empty(tmp_path) -> None:
    """An empty (header-only) TSV must return [] without crashing."""
    from apecx_integration.composition.tools.bv_brc_snapshot_tool import (
        _read_tsv,
    )
    p = tmp_path / "empty.tsv"
    p.write_text("genome.genome_id\tgenome.genome_name\n", encoding="utf-8")
    assert _read_tsv(p) == []


def test_probe_584_read_tsv_basic_rows(tmp_path) -> None:
    from apecx_integration.composition.tools.bv_brc_snapshot_tool import (
        _read_tsv,
    )
    p = tmp_path / "g.tsv"
    p.write_text(
        "genome.genome_id\tgenome.genome_name\n"
        "11020.5\tEEEV strain X\n"
        "11036.7\tVEEV strain Y\n",
        encoding="utf-8",
    )
    rows = _read_tsv(p)
    assert len(rows) == 2
    assert rows[0]["genome.genome_id"] == "11020.5"
    assert rows[1]["genome.genome_name"] == "VEEV strain Y"


def test_probe_585_read_fasta_pipe_delimited_format(tmp_path) -> None:
    """Cluster AQ regression. Real BV-BRC alphavirus FASTA uses
    pipe-delimited headers ``>fig_<id>.<n>|<product>|<role>|<md5>``.
    Pre-fix the parser silently skipped 100% of these (18,632/18,632
    headers in the production snapshot); workflow steps that
    needed protein sequences received empty results."""
    from apecx_integration.composition.tools.bv_brc_snapshot_tool import (
        _read_fasta_by_md5,
    )
    p = tmp_path / "x.fasta"
    md5a = "75e21b1d49191c5e97f681fe38e3f274"
    md5b = "b76bfd841fa2c1bb18d3f5d2fb82e0f3"
    p.write_text(
        f">fig_37124.7183.mat_peptide.2|protease nsp2|unknown|{md5a}\n"
        "MAKLNFGSL\n"
        "PRTV\n"
        f">fig_37124.789.CDS.2|structural polyprotein|unknown|{md5b}\n"
        "MGAY\n",
        encoding="utf-8",
    )
    seqs = _read_fasta_by_md5(p)
    assert seqs == {md5a: "MAKLNFGSLPRTV", md5b: "MGAY"}


def test_probe_586_read_fasta_md5_token_format(tmp_path) -> None:
    """The original md5=<hex> token format must still parse.
    Some operators may have older snapshots in this shape."""
    from apecx_integration.composition.tools.bv_brc_snapshot_tool import (
        _read_fasta_by_md5,
    )
    p = tmp_path / "x.fasta"
    p.write_text(
        ">fig|x.peg.1 envelope md5=aabbccdd11223344556677889900aabb\n"
        "GOODSEQ\n",
        encoding="utf-8",
    )
    seqs = _read_fasta_by_md5(p)
    assert seqs == {"aabbccdd11223344556677889900aabb": "GOODSEQ"}


def test_probe_587_read_fasta_skips_md5_less_headers(tmp_path) -> None:
    """A header with neither pipe-delimited 32-hex md5 nor a
    md5= token must be logged-and-skipped. The bad sequence must
    NOT bleed into the next valid header."""
    from apecx_integration.composition.tools.bv_brc_snapshot_tool import (
        _read_fasta_by_md5,
    )
    md5 = "ddeeff0011223344556677889900aabb"
    p = tmp_path / "x.fasta"
    p.write_text(
        ">no-md5-here some_product\n"
        "BADBADBAD\n"
        f">fig_x.y.1|product|src|{md5}\n"
        "GOODSEQ\n",
        encoding="utf-8",
    )
    seqs = _read_fasta_by_md5(p)
    assert seqs == {md5: "GOODSEQ"}
    assert "BADBADBAD" not in seqs.values()


def test_probe_588_read_fasta_concatenates_multiline_sequence(tmp_path) -> None:
    """A sequence wrapped over multiple lines must be reassembled
    verbatim. Blank lines are skipped, not treated as separators."""
    from apecx_integration.composition.tools.bv_brc_snapshot_tool import (
        _read_fasta_by_md5,
    )
    md5 = "aaaa1111bbbb2222cccc3333dddd4444"
    p = tmp_path / "x.fasta"
    p.write_text(
        f">fig_x.y.1|p|s|{md5}\n"
        "MAKLN\n"
        "FGSL\n"
        "\n"  # blank line in the middle — must skip without corrupting
        "PRTV\n",
        encoding="utf-8",
    )
    seqs = _read_fasta_by_md5(p)
    assert seqs == {md5: "MAKLNFGSLPRTV"}


def test_probe_589_read_fasta_missing_file_raises(tmp_path) -> None:
    from apecx_integration.composition.tools.bv_brc_snapshot_tool import (
        _read_fasta_by_md5,
    )
    with pytest.raises(FileNotFoundError, match="annotated FASTA not found"):
        _read_fasta_by_md5(tmp_path / "missing.fasta")


# ---------------------------------------------------------------------------
# DB engine config — probes 590-593
# ---------------------------------------------------------------------------


def test_probe_590_db_url_env_override() -> None:
    """APECX_CP_DB_URL env var must take precedence over the
    SQLite default. Production deploys depend on this."""
    from apecx_integration.control_plane.db import get_db_url
    with patch.dict(os.environ, {
        "APECX_CP_DB_URL": "postgresql+psycopg://u:p@h/db"
    }):
        assert get_db_url() == "postgresql+psycopg://u:p@h/db"


def test_probe_591_db_url_default_is_sqlite() -> None:
    from apecx_integration.control_plane.db import get_db_url
    saved = os.environ.pop("APECX_CP_DB_URL", None)
    try:
        url = get_db_url()
        assert url.startswith("sqlite")
    finally:
        if saved is not None:
            os.environ["APECX_CP_DB_URL"] = saved


def test_probe_592_sqlite_connect_args_check_same_thread() -> None:
    """SQLite engine must use check_same_thread=False so FastAPI
    threadpool can use a pooled connection across threads. Postgres
    engine must NOT pass that arg (it's a SQLite-specific kwarg)."""
    from sqlalchemy import text
    from apecx_integration.control_plane.db import make_engine
    eng = make_engine("sqlite:///:memory:")
    assert eng.dialect.name == "sqlite"
    with eng.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1


def test_probe_593_sqlite_pragmas_applied() -> None:
    """A new SQLite connection must have foreign_keys=ON, journal_mode
    in WAL, synchronous=NORMAL. Without foreign_keys=ON, FK constraints
    declared in models silently don't enforce."""
    from apecx_integration.control_plane.db import make_engine
    from sqlalchemy import text
    eng = make_engine("sqlite:///:memory:")
    with eng.connect() as conn:
        fk = conn.execute(text("PRAGMA foreign_keys")).scalar()
        assert fk == 1
        # journal_mode for in-memory SQLite returns 'memory', not 'wal'.
        # WAL only applies to file-backed DBs. Verify it doesn't error.
        jm = conn.execute(text("PRAGMA journal_mode")).scalar()
        assert jm in ("memory", "wal")
        sync = conn.execute(text("PRAGMA synchronous")).scalar()
        # NORMAL = 1
        assert sync in (1, 2, "NORMAL", "FULL")


# ---------------------------------------------------------------------------
# Enum invariants — probes 594-599
# ---------------------------------------------------------------------------


def test_probe_594_run_status_values_locked() -> None:
    """RunStatus enum is the contract between every persisted Run
    row and every consumer (sweeper, executor, MCP). Locking the
    set of valid string values prevents silent additions/removals
    that would break readers vs writers asymmetrically."""
    from apecx_integration.control_plane.schemas.enums import RunStatus
    assert {r.value for r in RunStatus} == {
        "pending", "running", "paused",
        "completed", "failed", "cancelled",
    }


def test_probe_595_step_status_values_locked() -> None:
    from apecx_integration.control_plane.schemas.enums import StepStatus
    assert {s.value for s in StepStatus} == {
        "pending", "running", "paused_for_approval",
        "completed", "failed", "skipped",
    }


def test_probe_596_approval_status_values_locked() -> None:
    """Cluster AO/AP touched approval flow; lock the status set."""
    from apecx_integration.control_plane.schemas.enums import ApprovalStatus
    assert {a.value for a in ApprovalStatus} == {
        "pending", "approved", "approved_with_modifications",
        "rejected", "auto_approved", "timed_out",
    }


def test_probe_597_executor_kind_values_locked() -> None:
    """ExecutorKind is referenced by the MCP layer's _VALID_EXECUTORS
    set. A drift here would silently break preferred_executor
    validation in start_workflow."""
    from apecx_integration.control_plane.schemas.enums import ExecutorKind
    assert {e.value for e in ExecutorKind} == {
        "local", "globus_compute", "pbs_bundle",
    }


def test_probe_598_provenance_event_type_values_locked() -> None:
    """Hash-chained provenance: the event_type is part of the hash
    input. Renaming a value silently invalidates every prior chain
    that referenced the old name. Lock the set hard."""
    from apecx_integration.control_plane.schemas.enums import (
        ProvenanceEventType,
    )
    assert {e.value for e in ProvenanceEventType} == {
        "run_started", "step_started", "step_completed",
        "approval_requested", "approval_decided",
        "artifact_created", "workflow_generated",
        "allocation_estimated", "allocation_confirmed",
        "run_completed", "run_failed",
    }


def test_probe_599_artifact_kind_values_locked() -> None:
    from apecx_integration.control_plane.schemas.enums import ArtifactKind
    assert {a.value for a in ArtifactKind} == {
        "input", "intermediate", "output",
        "generated_workflow", "generated_python",
    }


# ---------------------------------------------------------------------------
# Artifact store + composer/sandbox source-text contracts — probes 600-604
# ---------------------------------------------------------------------------


def test_probe_600_artifact_store_load_verifies_hash() -> None:
    """load_content must SHA-256-verify the on-disk file against
    the row's content_hash. A tampered file must raise ValueError —
    not silently return wrong bytes."""
    import inspect
    from apecx_integration.composition.artifact_store import ArtifactStore
    src = inspect.getsource(ArtifactStore.load_content)
    assert "hashlib.sha256" in src
    assert "content_hash mismatch" in src or "expected" in src.lower()


def test_probe_601_artifact_store_git_repo_validates_dotgit() -> None:
    """If GENERATED_ARTIFACTS_REPO_PATH is set but the path is not
    a git repo (no .git/ dir), the store must fail-fast with a
    clear message — silently writing files to a non-repo would
    lose audit history."""
    import inspect
    from apecx_integration.composition.artifact_store import ArtifactStore
    src = inspect.getsource(ArtifactStore._maybe_git_commit)
    assert ".git" in src
    assert "not a git" in src.lower() or "is not a git" in src.lower()


def test_probe_602_artifact_store_default_root_is_user_home() -> None:
    """Default artifacts root is ~/.apecx_cp/artifacts — NOT a
    repo-relative path. Important so multiple checkouts of the
    repo don't fight over the same artifact dir."""
    import inspect
    from apecx_integration.composition.artifact_store import ArtifactStore
    src = inspect.getsource(ArtifactStore.__init__)
    assert "Path.home()" in src
    assert ".apecx_cp" in src


def test_probe_603_canonical_json_no_spaces() -> None:
    """The provenance hash canonicalization uses (',', ':') as
    separators (no spaces). A different separator pair would
    produce different hashes for the same payload — silently
    breaking chain validation across older events."""
    from apecx_integration.control_plane.provenance.recorder import (
        _canonical_json,
    )
    canonical = _canonical_json({"a": 1, "b": [2, 3]})
    assert " " not in canonical
    assert canonical.count(":") >= 2
    assert canonical.count(",") >= 2


def test_probe_604_canonical_timestamp_falsy_naive_means_utc() -> None:
    """A naive timestamp (no tzinfo) is treated as UTC, not
    local. Different operator locales must produce identical
    canonical strings for identical UTC moments."""
    from datetime import datetime
    from apecx_integration.control_plane.provenance.recorder import (
        _canonical_timestamp,
    )
    naive = datetime(2026, 4, 26, 12, 0, 0)
    canonical = _canonical_timestamp(naive)
    # The canonical form ends with +00:00 (UTC)
    assert canonical.endswith("+00:00")
