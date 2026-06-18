"""prune_artifacts — bounded FIFO retention for ~/.apecx/artifacts/<run_id>/ (keeps it from
growing forever; 432 dirs had accumulated with zero pruning before this)."""

from __future__ import annotations

import os

from apecx_integration.mcp_surface.tools.eo_primitives import prune_artifacts


def _make_run_dir(base, name, mtime):
    d = base / name
    (d / "figures").mkdir(parents=True)
    (d / "report.md").write_text("# r", encoding="utf-8")
    os.utime(d, (mtime, mtime))
    return d


def test_prunes_to_n_newest_by_mtime(tmp_path):
    base = tmp_path / "artifacts"
    base.mkdir()
    # 8 run dirs with increasing mtime (run0 oldest ... run7 newest).
    for i in range(8):
        _make_run_dir(base, f"run{i}", mtime=1_000_000 + i * 10)

    prune_artifacts(base, max_runs=3)

    survivors = sorted(p.name for p in base.iterdir() if p.is_dir())
    assert survivors == ["run5", "run6", "run7"], survivors  # 3 newest kept
    assert not (base / "run0").exists()


def test_noop_when_under_cap(tmp_path):
    base = tmp_path / "artifacts"
    base.mkdir()
    for i in range(2):
        _make_run_dir(base, f"run{i}", mtime=1_000_000 + i)
    prune_artifacts(base, max_runs=5)
    assert len(list(base.iterdir())) == 2


def test_never_raises_on_missing_base(tmp_path):
    # Missing base dir + a bad cap: must not raise.
    prune_artifacts(tmp_path / "does-not-exist", max_runs=3)
    prune_artifacts(tmp_path / "does-not-exist", max_runs=0)


def test_env_cap_default(tmp_path, monkeypatch):
    base = tmp_path / "artifacts"
    base.mkdir()
    for i in range(4):
        _make_run_dir(base, f"run{i}", mtime=1_000_000 + i)
    monkeypatch.setenv("APECX_ARTIFACTS_MAX_RUNS", "2")
    prune_artifacts(base)  # cap from env
    assert len(list(base.iterdir())) == 2
