"""T6: build_env_manifest provenance stamp. Real git on tmp repos, no mocks."""

from __future__ import annotations

import subprocess

from apecx_integration.composition.env_manifest import (
    _compute_reproducible,
    _git_state,
    build_env_manifest,
)


def _git_init(d):
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(d), "config", "user.name", "t"], check=True)


def test_compute_reproducible_logic():
    assert _compute_reproducible({"a": {"sha": "abc", "dirty": False, "vcs": True}}) is True
    # ANY dirty repo -> not reproducible (clean SHA over a dirty tree is deceptive).
    assert _compute_reproducible({"a": {"sha": "abc", "dirty": True, "vcs": True}}) is False
    # Missing SHA (no VCS) -> can't pin -> not reproducible.
    assert _compute_reproducible({"a": {"sha": None, "dirty": None, "vcs": False}}) is False
    assert _compute_reproducible({}) is False
    # One clean + one dirty -> not reproducible.
    assert (
        _compute_reproducible(
            {
                "a": {"sha": "abc", "dirty": False, "vcs": True},
                "b": {"sha": "def", "dirty": True, "vcs": True},
            }
        )
        is False
    )


def test_git_state_clean_then_dirty(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    _git_init(d)
    (d / "f.txt").write_text("hi")
    subprocess.run(["git", "-C", str(d), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(d), "commit", "-qm", "init"], check=True)
    st = _git_state(d)
    assert st["vcs"] is True and st["sha"] and st["dirty"] is False
    (d / "f.txt").write_text("changed")  # uncommitted edit
    assert _git_state(d)["dirty"] is True


def test_git_state_non_git_dir(tmp_path):
    assert _git_state(tmp_path)["vcs"] is False


def test_persisted_summary_includes_env_manifest():
    # The persisted GeneratedArtifact composition_summary MUST carry env_manifest, or
    # Project B could never read the stamp (review-gate "dropped at persistence" defect).
    # Tests the extracted serializer directly — spec-rot-independent (the store-backed
    # compose tests are pre-existing-broken by spec-mode rot).
    from apecx_integration.composition.composer import _persisted_composition_summary
    from apecx_integration.composition.composer_schemas import CompositionSummary

    manifest = {"repos": {}, "reproducible": False}
    summary = CompositionSummary(
        steps_reused=1,
        steps_generated=0,
        steps_swapped=0,
        summary_sentence="s",
        env_manifest=manifest,
    )
    persisted = _persisted_composition_summary(summary, {})
    assert persisted["env_manifest"] == manifest
    assert persisted["steps_reused"] == 1
    assert "step_categorizations" in persisted and "class_path_repairs" in persisted


def test_build_env_manifest_structure():
    m = build_env_manifest(llm_model="devstral-small-2:latest", llm_base_url="http://x/v1")
    assert set(m["repos"]) == {"apecx-integration", "nanobrain"}
    assert set(m["key_packages"]) == {"pydantic", "nanobrain", "faiss-cpu", "sentence-transformers"}
    assert m["llm"] == {"model": "devstral-small-2:latest", "base_url": "http://x/v1"}
    assert isinstance(m["reproducible"], bool)
    assert m["python"] and m["platform"]
    # apecx-integration resolves to this editable checkout -> has a SHA.
    assert m["repos"]["apecx-integration"]["sha"]
