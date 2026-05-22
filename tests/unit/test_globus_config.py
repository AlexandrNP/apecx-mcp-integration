"""Unit tests for the persistent Globus config (~/.apecx/globus_config.json)."""

from __future__ import annotations

import json

import pytest

from apecx_integration.cli import globus_config


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    """Point the config at a temp file so tests never touch ~/.apecx."""
    cfg = tmp_path / "globus_config.json"
    monkeypatch.setenv("APECX_GLOBUS_CONFIG_PATH", str(cfg))
    return cfg


def test_load_absent_returns_defaults():
    cfg = globus_config.load()
    assert cfg == {"dest_endpoint_id": None, "extra_source_dirs": []}


def test_set_and_get_dest_endpoint(_isolate_config):
    globus_config.set_dest_endpoint("  abc-123  ")
    assert globus_config.get_dest_endpoint() == "abc-123"
    assert json.loads(_isolate_config.read_text())["dest_endpoint_id"] == "abc-123"


def test_add_source_dir_defaults_subdir_from_basename():
    entry = globus_config.add_source_dir("/apecx-ramanathan-anl/foo/bar/")
    assert entry == {"remote_path": "/apecx-ramanathan-anl/foo/bar", "dest_subdir": "bar"}
    assert globus_config.get_extra_source_dirs() == [entry]


def test_add_source_dir_explicit_subdir():
    entry = globus_config.add_source_dir("/x/y", dest_subdir="custom")
    assert entry["dest_subdir"] == "custom"


def test_add_source_dir_idempotent_updates_in_place():
    globus_config.add_source_dir("/x/y", dest_subdir="first")
    globus_config.add_source_dir("/x/y", dest_subdir="second")
    dirs = globus_config.get_extra_source_dirs()
    assert len(dirs) == 1
    assert dirs[0]["dest_subdir"] == "second"


def test_add_two_distinct_dirs():
    globus_config.add_source_dir("/a/one")
    globus_config.add_source_dir("/a/two")
    remotes = {d["remote_path"] for d in globus_config.get_extra_source_dirs()}
    assert remotes == {"/a/one", "/a/two"}


def test_unknown_top_key_fails_loud(_isolate_config):
    _isolate_config.write_text(json.dumps({"dest_endpoint_id": "x", "typo_key": 1}))
    with pytest.raises(ValueError, match="unknown key"):
        globus_config.load()


def test_unknown_dir_key_fails_loud(_isolate_config):
    _isolate_config.write_text(
        json.dumps({"extra_source_dirs": [{"remote_path": "/a", "bogus": 1}]})
    )
    with pytest.raises(ValueError, match="unknown key"):
        globus_config.load()


def test_empty_remote_path_fails_loud():
    with pytest.raises(ValueError, match="remote_path"):
        globus_config.add_source_dir("   ")


def test_malformed_json_fails_loud(_isolate_config):
    _isolate_config.write_text("{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        globus_config.load()


def test_save_is_atomic_and_roundtrips(_isolate_config):
    globus_config.set_dest_endpoint("dest-1")
    globus_config.add_source_dir("/p/q", dest_subdir="qq")
    reloaded = globus_config.load()
    assert reloaded["dest_endpoint_id"] == "dest-1"
    assert reloaded["extra_source_dirs"] == [{"remote_path": "/p/q", "dest_subdir": "qq"}]
