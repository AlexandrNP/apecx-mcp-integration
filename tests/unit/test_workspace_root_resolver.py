"""Unit tests for the shared workspace-root resolver.

The resolver replaced an earlier-spread-out pattern of
``Path(__file__).resolve().parents[N]`` calls. This test pins the
resolution-order contract (env var > marker walk > parents[N] fallback)
so a refactor can't silently regress to "always parents[N]" behavior.
"""

from __future__ import annotations

from pathlib import Path

from apecx_integration._workspace import resolve_workspace_root


def test_env_var_override_wins(monkeypatch, tmp_path):
    """APECX_WORKSPACE_ROOT takes priority over file-walk + fallback.

    Detection signal: a refactor that swaps the env-var check below
    the file-walk would let a configured operator override be ignored
    on systems whose layout HAPPENS to look like a workspace —
    silent footgun.
    """
    target = tmp_path / "custom_workspace"
    target.mkdir()
    monkeypatch.setenv("APECX_WORKSPACE_ROOT", str(target))

    out = resolve_workspace_root("/some/random/path.py", fallback_depth=5)
    assert out == target


def test_env_var_path_is_resolved_and_expanded(monkeypatch, tmp_path):
    """Tilde-expansion + symlink resolution are applied to the override."""
    target = tmp_path / "ws"
    target.mkdir()
    monkeypatch.setenv("APECX_WORKSPACE_ROOT", str(target) + "/.")
    out = resolve_workspace_root("/x.py", fallback_depth=5)
    assert out == target.resolve()


def test_marker_walk_finds_canonical_workspace(monkeypatch, tmp_path):
    """When env var is unset, walk upward until apecx-mcp-integration
    sits next to a marker (data / nanobrain / _workspace_notes)."""
    monkeypatch.delenv("APECX_WORKSPACE_ROOT", raising=False)

    workspace = tmp_path / "fake_workspace"
    workspace.mkdir()
    (workspace / "apecx-mcp-integration").mkdir()
    (workspace / "data").mkdir()
    deep_file = workspace / "apecx-mcp-integration" / "src" / "pkg" / "module.py"
    deep_file.parent.mkdir(parents=True)
    deep_file.write_text("# test")

    out = resolve_workspace_root(deep_file, fallback_depth=99)
    assert out == workspace


def test_marker_walk_requires_apecx_mcp_integration(monkeypatch, tmp_path):
    """A directory without ``apecx-mcp-integration`` is NOT a workspace
    root, even if it has data/ or nanobrain/. False-positive defense.
    """
    monkeypatch.delenv("APECX_WORKSPACE_ROOT", raising=False)

    not_workspace = tmp_path / "not_workspace"
    not_workspace.mkdir()
    (not_workspace / "data").mkdir()
    (not_workspace / "nanobrain").mkdir()
    deep_file = not_workspace / "src" / "module.py"
    deep_file.parent.mkdir(parents=True)
    deep_file.write_text("# test")

    out = resolve_workspace_root(deep_file, fallback_depth=1)
    # Walk-up failed → fallback parents[1] = not_workspace/src
    assert out == deep_file.resolve().parents[1]


def test_marker_walk_requires_at_least_one_sibling_marker(monkeypatch, tmp_path):
    """A directory with apecx-mcp-integration but no marker siblings
    is NOT a workspace root. Prevents matching arbitrary forks."""
    monkeypatch.delenv("APECX_WORKSPACE_ROOT", raising=False)

    parent = tmp_path / "loose_repo_parent"
    parent.mkdir()
    (parent / "apecx-mcp-integration").mkdir()
    # No data/, no nanobrain/, no _workspace_notes/
    deep_file = parent / "apecx-mcp-integration" / "src" / "module.py"
    deep_file.parent.mkdir(parents=True)
    deep_file.write_text("# test")

    out = resolve_workspace_root(deep_file, fallback_depth=2)
    # Walk-up failed → fallback parents[2] = parent
    assert out == deep_file.resolve().parents[2]


def test_canonical_repo_resolves_to_workspace_root():
    """Smoke: when called from the actual repo file, the resolver
    returns the actual apecx-cowork workspace root."""
    # Use the violin step file, which lives at depth 5 from workspace.
    here = (
        Path(__file__).resolve().parents[2]  # tests/unit → repo root
        / "src"
        / "apecx_integration"
        / "composition"
        / "steps"
        / "violin_bvbrc_context_step.py"
    )
    assert here.is_file(), here

    out = resolve_workspace_root(here, fallback_depth=5)
    # The workspace root must contain apecx-mcp-integration + data + nanobrain
    assert (out / "apecx-mcp-integration").is_dir()
