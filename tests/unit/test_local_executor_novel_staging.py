"""#1c Phase 3 — LocalExecutor._stage_workflow materializes novel-step file-path config.

When the composed YAML carries `_apecx_sandboxed_novel_config` (the spec expander routed novel steps
through SandboxedNovelStep), the stager must (1) build a REAL steps/ dir — copying the catalog steps +
writing each novel per-step config YAML — because a symlinked read-only catalog can't take new files,
and (2) STRIP the metadata key so Workflow.from_config never sees it. Without novel steps, the cheap
symlink path is unchanged. `_stage_workflow` only reads self._workflow_base_dir, so a stub self suffices
(no DB / no docker)."""

from __future__ import annotations

import yaml

from apecx_integration.control_plane.executors.local import LocalExecutor


class _Stub:
    def __init__(self, base):
        self._workflow_base_dir = base


def test_stage_materializes_novel_config_and_strips_metadata_key(tmp_path):
    base = tmp_path / "base"
    (base / "steps").mkdir(parents=True)
    (base / "steps" / "lib.yml").write_text("class: X\nname: lib\n")  # a catalog step

    wf = {
        "name": "w",
        "config_version": 2,
        "steps": {
            "novel1": {
                "class": "apecx_integration.composition.steps.sandboxed_novel_step.SandboxedNovelStep",
                "config": "steps/novel1.yml",
            }
        },
        "links": {},
        "_apecx_sandboxed_novel_config": {
            "novel1": {
                "class": "apecx_integration.composition.steps.sandboxed_novel_step.SandboxedNovelStep",
                "name": "novel1",
                "novel_source": "class Foo: ...",
                "target_class_name": "Foo",
                "step_config": {},
            }
        },
    }
    yaml_path = tmp_path / "artifact.yml"
    yaml_path.write_text(yaml.safe_dump(wf))
    run_root = tmp_path / "run"
    run_root.mkdir()

    staged = LocalExecutor._stage_workflow(_Stub(base), yaml_path, run_root)

    # (2) metadata key stripped from the staged workflow.
    staged_doc = yaml.safe_load(staged.read_text())
    assert "_apecx_sandboxed_novel_config" not in staged_doc
    assert staged_doc["steps"]["novel1"]["config"] == "steps/novel1.yml"

    # (1) a REAL steps/ dir (not a symlink) with the novel config materialized + catalog copied in.
    steps_dir = run_root / "steps"
    assert steps_dir.is_dir() and not steps_dir.is_symlink()
    assert (steps_dir / "lib.yml").is_file()  # catalog step copied alongside
    novel = yaml.safe_load((steps_dir / "novel1.yml").read_text())
    assert novel["target_class_name"] == "Foo"
    assert novel["novel_source"] == "class Foo: ..."
    assert novel["name"] == "novel1"


def test_stage_refuses_traversal_step_id(tmp_path):
    """#1c defense-in-depth: even if a traversal step id reached the stager (the spec validator is the
    primary guard), _stage_workflow must refuse to write outside steps/ — never clobber a trusted
    catalog YAML or drop an attacker YAML on the host."""
    import pytest

    base = tmp_path / "base"
    (base / "steps").mkdir(parents=True)
    wf = {
        "name": "w",
        "steps": {},
        "links": {},
        "_apecx_sandboxed_novel_config": {
            "../../evil": {
                "class": "X",
                "name": "evil",
                "novel_source": "bad",
                "target_class_name": "E",
            }
        },
    }
    yaml_path = tmp_path / "a.yml"
    yaml_path.write_text(yaml.safe_dump(wf))
    run_root = tmp_path / "run"
    run_root.mkdir()
    with pytest.raises(ValueError, match="outside steps/"):
        LocalExecutor._stage_workflow(_Stub(base), yaml_path, run_root)


def test_stage_symlinks_steps_when_no_novel_config(tmp_path):
    base = tmp_path / "base"
    (base / "steps").mkdir(parents=True)
    (base / "steps" / "lib.yml").write_text("class: X\nname: lib\n")
    yaml_path = tmp_path / "a.yml"
    yaml_path.write_text(yaml.safe_dump({"name": "w", "steps": {}, "links": {}}))
    run_root = tmp_path / "run"
    run_root.mkdir()

    LocalExecutor._stage_workflow(_Stub(base), yaml_path, run_root)
    # Unchanged behavior: steps/ is a symlink to the catalog (cheap, no copy).
    assert (run_root / "steps").is_symlink()
