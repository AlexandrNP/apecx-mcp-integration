"""Unit tests for the MCP discovery tools (Option A).

Hits the real packaged composer config + the real
``workflows/violin_bvbrc/manifest.yml`` — no mocking. The
discovery tools are pure file readers; "real" here means the
shipped manifest, not a constructed fixture.

A constructed-fixture test is added separately for the
non-default-config path (env-var override + multi-manifest case).
"""

from __future__ import annotations

import asyncio

import pytest
import yaml

from apecx_integration.mcp_surface.tools import discovery

# ---------------------------------------------------------------------------
# Default packaged config — exercises the real manifest
# ---------------------------------------------------------------------------


def test_list_workflows_returns_violin_bvbrc():
    out = asyncio.run(discovery.list_workflows())
    assert out["count"] >= 1
    names = [row["workflow_name"] for row in out["workflows"]]
    assert "violin_bvbrc_synonym_gate" in names


def test_list_workflows_row_shape():
    out = asyncio.run(discovery.list_workflows())
    row = next(r for r in out["workflows"] if r["workflow_name"] == "violin_bvbrc_synonym_gate")
    # Required fields present + types are JSON-friendly
    assert isinstance(row["manifest_path"], str)
    assert row["manifest_path"].endswith("manifest.yml")
    assert row["num_components"] >= 9
    assert row["num_ready"] >= 1
    assert row["num_deferred"] >= 0
    assert row["first_release_variant"] == "hard_only"
    assert isinstance(row["component_names"], list)
    assert "entity_extraction" in row["component_names"]


def test_describe_workflow_returns_full_components():
    out = asyncio.run(discovery.describe_workflow("violin_bvbrc_synonym_gate"))
    assert "error" not in out
    assert out["workflow_name"] == "violin_bvbrc_synonym_gate"
    components = out["components"]
    # Must include the deferred entry too — discovery shows the full picture.
    names = {c["step_name"] for c in components}
    assert "entity_extraction" in names
    assert "synonym_fuzzy_match" in names

    # Per-component shape check using a known entry.
    extraction = next(c for c in components if c["step_name"] == "entity_extraction")
    assert extraction["step_id"] == "1"
    assert extraction["disposition"] == "wrap"
    assert extraction["status"] == "ready"
    assert extraction["rag_description"]
    assert extraction["rag_examples"]
    # Multi-line YAML folded scalars must collapse to single-spaced strings.
    assert "  " not in extraction["rag_description"]


def test_describe_workflow_unknown_returns_structured_error():
    out = asyncio.run(discovery.describe_workflow("does_not_exist"))
    assert "error" in out
    assert "does_not_exist" in out["error"]
    assert "available" in out
    assert "violin_bvbrc_synonym_gate" in out["available"]


def test_describe_workflow_empty_name_is_an_error_not_a_crash():
    out = asyncio.run(discovery.describe_workflow(""))
    assert "error" in out
    assert "available" in out


def test_describe_workflow_reports_deferred_status():
    """The deferred synonym_fuzzy_match must surface as deferred so
    the model doesn't re-propose it via start_workflow."""
    out = asyncio.run(discovery.describe_workflow("violin_bvbrc_synonym_gate"))
    fuzzy = next(c for c in out["components"] if c["step_name"] == "synonym_fuzzy_match")
    assert fuzzy["disposition"] == "deferred"


# ---------------------------------------------------------------------------
# Non-default config — env-var override + multi-manifest behavior
# ---------------------------------------------------------------------------


def test_apecx_composer_config_env_var_override(tmp_path, monkeypatch):
    """An operator override via APECX_COMPOSER_CONFIG must redirect
    discovery to the alternate config + its manifests."""
    manifest = tmp_path / "fake_workflow" / "manifest.yml"
    manifest.parent.mkdir()
    manifest.write_text(
        yaml.safe_dump(
            {
                "workflow": {
                    "name": "fake_for_test",
                    "spec": "docs/fake_spec.md",
                    "first_release_variant": "v1",
                },
                "components": [
                    {
                        "step_id": "1",
                        "step_name": "fake_step",
                        "disposition": "new",
                        "status": "ready",
                        "class": "fake.module.FakeStep",
                        "yaml": "steps/fake.yml",
                        "rag_description": "A   fake   step\nfor testing.",
                        "rag_examples": ["example one", "example two"],
                    },
                ],
            }
        )
    )
    cfg = tmp_path / "composer_config.yml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "component_catalog_paths": ["fake_workflow/manifest.yml"],
            }
        )
    )

    monkeypatch.setenv("APECX_COMPOSER_CONFIG", str(cfg))

    out = asyncio.run(discovery.list_workflows())
    names = [row["workflow_name"] for row in out["workflows"]]
    assert names == ["fake_for_test"]

    desc = asyncio.run(discovery.describe_workflow("fake_for_test"))
    assert desc["components"][0]["step_name"] == "fake_step"
    # Whitespace collapse: the multi-line description must become one line.
    assert desc["components"][0]["rag_description"] == "A fake step for testing."


def test_apecx_composer_config_missing_file_raises(monkeypatch):
    monkeypatch.setenv("APECX_COMPOSER_CONFIG", "/nope/does/not/exist.yml")
    with pytest.raises(FileNotFoundError):
        asyncio.run(discovery.list_workflows())


# ---------------------------------------------------------------------------
# Server registration
# ---------------------------------------------------------------------------


def test_build_server_registers_discovery_tools():
    from apecx_integration.mcp_surface.server import build_server

    server = build_server()
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert "list_workflows" in names
    assert "describe_workflow" in names


# ---------------------------------------------------------------------------
# EO-01 — list_workflows also surfaces the runnable catalog (run_workflow targets)
# ---------------------------------------------------------------------------


def test_list_workflows_includes_runnable_catalog():
    out = asyncio.run(discovery.list_workflows())
    assert "runnable" in out and "runnable_count" in out
    assert out["runnable_count"] == len(out["runnable"])
    # The packaged runnable catalog ships rhea_muscle_alignment.
    names = {r["name"] for r in out["runnable"]}
    assert "rhea_muscle_alignment" in names
    # No load error on the packaged catalog.
    assert "runnable_error" not in out


def test_runnable_row_shape_and_availability_flag():
    out = asyncio.run(discovery.list_workflows())
    row = next(r for r in out["runnable"] if r["name"] == "rhea_muscle_alignment")
    assert row["kind"] == "runnable"
    assert row["invoke_with"] == "run_workflow"
    assert isinstance(row["description"], str) and row["description"]
    # availability is computed from prerequisites — a bool + a list, env-dependent value.
    assert isinstance(row["available"], bool)
    assert isinstance(row["missing_prerequisites"], list)
    assert isinstance(row["input_schema"], dict)
    # When RHEA isn't configured, the row honestly reports WHY it can't run.
    if not row["available"]:
        assert row["missing_prerequisites"], "unavailable row must name its missing prereqs"


def test_composable_rows_tagged_for_compose_path():
    out = asyncio.run(discovery.list_workflows())
    row = next(r for r in out["workflows"] if r["workflow_name"] == "violin_bvbrc_synonym_gate")
    assert row["kind"] == "composable"
    assert row["invoke_with"] == "start_workflow"
