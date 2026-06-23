"""T5: ComponentCatalog.from_manifests collision hard-fail.

rich_id (workflow_slug/name:step_id) is the identity primitive; a duplicate id
pointing at a DIFFERENT wrapper file is the silent-shadowing bug -> now FAIL LOUD.
Same id + same file -> idempotent collapse. Real manifests/files, no mocks.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import apecx_integration
from apecx_integration.composition.component_catalog import ComponentCatalog

_CFGDIR = Path(apecx_integration.__file__).parent / "composition"


def _manifest(dir_path: Path, step_id: str, yaml_rel: str) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "manifest.yml").write_text(
        "components:\n"
        f"  - step_id: {step_id}\n"
        f"    step_name: {step_id}\n"
        "    class: pkg.mod.SomeStep\n"
        f"    yaml: {yaml_rel}\n"
        "    rag_description: a reusable test step\n",
        encoding="utf-8",
    )
    return dir_path / "manifest.yml"


def test_real_catalog_loads_without_collision():
    # False-positive guard: the real 19-component catalog must load cleanly.
    cfg = yaml.safe_load((_CFGDIR / "composer_config.yml").read_text())
    cat = ComponentCatalog.from_manifests(
        [(_CFGDIR / p).resolve() for p in cfg["component_catalog_paths"]]
    )
    assert len(cat.components) == 19


def test_same_id_different_file_raises(tmp_path):
    # Two manifests under same-named dirs -> same workflow_slug -> same rich_id;
    # distinct wrapper files -> FAIL LOUD (the shadowing bug).
    (tmp_path / "a" / "wf" / "steps").mkdir(parents=True)
    (tmp_path / "a" / "wf" / "steps" / "x.yml").write_text("name: x\n")
    (tmp_path / "b" / "wf" / "steps").mkdir(parents=True)
    (tmp_path / "b" / "wf" / "steps" / "x.yml").write_text("name: y\n")
    m1 = _manifest(tmp_path / "a" / "wf", "S1", "steps/x.yml")
    m2 = _manifest(tmp_path / "b" / "wf", "S1", "steps/x.yml")
    with pytest.raises(ValueError, match="duplicate component id"):
        ComponentCatalog.from_manifests([m1, m2])


def test_same_id_same_file_collapses(tmp_path):
    # Same rich_id + same resolved file (component listed twice) -> idempotent collapse.
    shared = tmp_path / "shared" / "x.yml"
    shared.parent.mkdir(parents=True)
    shared.write_text("name: x\n")
    m1 = _manifest(tmp_path / "a" / "wf", "S1", "../../shared/x.yml")
    m2 = _manifest(tmp_path / "b" / "wf", "S1", "../../shared/x.yml")
    cat = ComponentCatalog.from_manifests([m1, m2])
    assert len(cat.components) == 1


def test_unresolved_entry_not_treated_as_collision(tmp_path):
    # Two same-id entries whose yaml doesn't resolve (abs=None) -> skip path
    # check, collapse (don't mask an unresolvable entry as a collision-raise).
    m1 = _manifest(tmp_path / "a" / "wf", "S1", "steps/missing.yml")
    m2 = _manifest(tmp_path / "b" / "wf", "S1", "steps/missing.yml")
    cat = ComponentCatalog.from_manifests([m1, m2])
    assert len(cat.components) == 1
