"""Unit tests for the T05 PBS bundle generator.

No FastAPI / no SQLAlchemy — exercise ``generate_bundle`` directly
against a real tempdir and verify bundle structure.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from apecx_integration.execution.pbs_bundle import (
    BundleRequest,
    UnsupportedSystem,
    generate_bundle,
)

SAMPLE_YAML = """\
name: bundle_test_wf
description: "test"
version: "0.1"
steps:
  extract:
    class: "pkg.library.A"
    config: "steps/a.yml"
links: {}
"""


def _make_request(
    tmp_path: Path, *, target_system: str = "polaris"
) -> BundleRequest:
    tmp_path.mkdir(parents=True, exist_ok=True)
    wf = tmp_path / "input_workflow.yml"
    wf.write_text(SAMPLE_YAML)
    return BundleRequest(
        run_id=uuid4(),
        target_system=target_system,
        output_directory=tmp_path / "bundle",
        workflow_yaml_path=wf,
        library_version="0.1.0-test",
        llm_model="mistral-nemo:latest",
        artifact_id=uuid4(),
        composition_summary_sentence=(
            "This workflow has 1 step(s). 1 compose library components "
            "(1 standard + 0 parameterized + 0 wrapped)."
        ),
    )


def test_bundle_contains_all_required_files(tmp_path: Path):
    result = generate_bundle(_make_request(tmp_path))
    assert result.bundle_path.is_dir()
    for required in (
        "submit.pbs",
        "run.sh",
        "workflow.yml",
        "staging_plan.yml",
        "provenance_seed.json",
        "README.md",
    ):
        assert (result.bundle_path / required).is_file(), (
            f"bundle missing required file: {required}"
        )


def test_submit_pbs_is_executable_and_qsub_shaped(tmp_path: Path):
    result = generate_bundle(_make_request(tmp_path))
    submit = result.bundle_path / "submit.pbs"
    content = submit.read_text()
    # PBS directives present.
    for directive in ("#PBS -N", "#PBS -A", "#PBS -q", "#PBS -l"):
        assert directive in content, f"submit.pbs missing {directive}"
    # Body invokes run.sh.
    assert "bash run.sh" in content
    # Executable bit set.
    assert submit.stat().st_mode & 0o111, (
        "submit.pbs should be executable"
    )


def test_run_sh_is_executable_and_writes_markers(tmp_path: Path):
    result = generate_bundle(_make_request(tmp_path))
    run_sh = result.bundle_path / "run.sh"
    content = run_sh.read_text()
    assert content.startswith("#!/bin/bash")
    assert "apecx_status.txt" in content
    assert "outputs/result.json" in content
    assert run_sh.stat().st_mode & 0o111


def test_workflow_yaml_copied_verbatim(tmp_path: Path):
    request = _make_request(tmp_path)
    result = generate_bundle(request)
    staged = (result.bundle_path / "workflow.yml").read_text()
    assert staged == SAMPLE_YAML


def test_provenance_seed_carries_reingest_metadata(tmp_path: Path):
    request = _make_request(tmp_path)
    result = generate_bundle(request)
    seed = json.loads(
        (result.bundle_path / "provenance_seed.json").read_text()
    )
    assert seed["run_id"] == str(request.run_id)
    assert seed["artifact_id"] == str(request.artifact_id)
    assert seed["library_version"] == "0.1.0-test"
    assert seed["llm_model"] == "mistral-nemo:latest"
    assert seed["target_system"] == "polaris"
    assert seed["generated_at"]


def test_readme_is_self_contained(tmp_path: Path):
    result = generate_bundle(_make_request(tmp_path))
    readme = (result.bundle_path / "README.md").read_text()
    # AP §5.5 AC4: a first-time reader must be able to orient themselves.
    assert "qsub submit.pbs" in readme
    assert "provenance_seed.json" in readme
    assert "allocation" in readme.lower()


def test_unsupported_target_system_raises(tmp_path: Path):
    with pytest.raises(UnsupportedSystem, match="not supported"):
        generate_bundle(_make_request(tmp_path, target_system="frontier"))


def test_missing_workflow_yaml_raises(tmp_path: Path):
    request = BundleRequest(
        run_id=uuid4(),
        target_system="polaris",
        output_directory=tmp_path / "bundle",
        workflow_yaml_path=tmp_path / "does_not_exist.yml",
        library_version="0.1.0",
        llm_model="m",
        artifact_id=uuid4(),
        composition_summary_sentence="x",
    )
    with pytest.raises(FileNotFoundError):
        generate_bundle(request)


def test_polaris_vs_aurora_queue_differs(tmp_path: Path):
    polaris = generate_bundle(_make_request(tmp_path / "a", target_system="polaris"))
    aurora = generate_bundle(_make_request(tmp_path / "b", target_system="aurora"))
    polaris_pbs = (polaris.bundle_path / "submit.pbs").read_text()
    aurora_pbs = (aurora.bundle_path / "submit.pbs").read_text()
    assert "-q prod" in polaris_pbs
    assert "-q EarlyAppAccess" in aurora_pbs


def test_submit_command_is_copy_pasteable(tmp_path: Path):
    result = generate_bundle(_make_request(tmp_path))
    assert "qsub submit.pbs" in result.submit_command
    assert str(result.bundle_path) in result.submit_command
