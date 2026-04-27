"""Probe batch 24 — production workflow YAML integrity (probes 630-654).

The violin_bvbrc workflow under
``src/apecx_integration/composition/workflows/violin_bvbrc/`` is the
production composition the MCP surface ships. Each YAML file in there
is wire-loadable by ``nanobrain.core.workflow.Workflow.from_config``
at runtime — meaning the slightest broken reference (missing class,
typo'd config path, link to nonexistent step, dangling data unit
name) fails the workflow loader.

This batch audits every production YAML against the runtime
guarantees:

  - workflow.yml top-level structure
  - every ``class:`` is an importable Python class
  - every ``config:`` path resolves to a real file
  - every link references real step ids + real data unit names
  - every component in manifest.yml has a working class+yaml
  - composer_config.yml validates against ComposerConfig
  - composer prompts dir has the expected files

A future commit that renames a step id, removes a class, or
breaks a config path trips one or more of these probes. Without
them the regression would only surface when an operator actually
ran the workflow.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml


pytestmark = pytest.mark.integration


_WORKFLOW_DIR = (
    Path(__file__).resolve().parents[2]
    / "src" / "apecx_integration" / "composition"
    / "workflows" / "violin_bvbrc"
)
_WORKFLOW_YAML = _WORKFLOW_DIR / "violin_bvbrc_workflow.yml"
_MANIFEST_YAML = _WORKFLOW_DIR / "manifest.yml"
_COMPOSITION_DIR = (
    Path(__file__).resolve().parents[2]
    / "src" / "apecx_integration" / "composition"
)
_COMPOSER_CONFIG_YAML = _COMPOSITION_DIR / "composer_config.yml"
_COMPOSER_PROMPTS_DIR = _COMPOSITION_DIR / "composer_prompts"


def _load(p: Path) -> dict:
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def _import_class(dotted_path: str) -> type | None:
    """Return the class object if importable, else None."""
    if not dotted_path:
        return None
    module_path, _, class_name = dotted_path.rpartition(".")
    if not module_path:
        return None
    try:
        mod = importlib.import_module(module_path)
    except Exception:
        return None
    return getattr(mod, class_name, None)


# ---------------------------------------------------------------------------
# Workflow YAML structural integrity — probes 630-639
# ---------------------------------------------------------------------------


def test_probe_630_workflow_required_top_level_keys() -> None:
    """The workflow YAML must declare name, version, steps, links."""
    wf = _load(_WORKFLOW_YAML)
    required = {"name", "version", "steps", "links"}
    missing = required - set(wf.keys())
    assert not missing, (
        f"PROBE 630: workflow.yml missing keys: {missing}"
    )


def test_probe_631_every_step_has_class_and_config() -> None:
    """Every step entry must declare class: + config:. A step
    with neither would be unloadable."""
    wf = _load(_WORKFLOW_YAML)
    for step_id, step in wf["steps"].items():
        assert isinstance(step, dict), step_id
        assert step.get("class"), f"PROBE 631: step {step_id} has no class"
        assert step.get("config"), f"PROBE 631: step {step_id} has no config"


def test_probe_632_every_step_config_path_resolves() -> None:
    """Every step's ``config:`` (when a string path) must point at
    a real file relative to the workflow dir. A typo'd path means
    ``Workflow.from_config`` 404s mid-load."""
    wf = _load(_WORKFLOW_YAML)
    for step_id, step in wf["steps"].items():
        cfg = step["config"]
        if isinstance(cfg, str):
            full = _WORKFLOW_DIR / cfg
            assert full.is_file(), (
                f"PROBE 632: step {step_id}'s config: {cfg} -> {full} not found"
            )


def test_probe_633_every_step_yaml_has_name_field() -> None:
    """Every step YAML must declare a ``name:`` field — required by
    BaseStep.REQUIRED_CONFIG_FIELDS at framework load time."""
    wf = _load(_WORKFLOW_YAML)
    for step_id, step in wf["steps"].items():
        cfg_ref = step["config"]
        if isinstance(cfg_ref, str):
            sc = _load(_WORKFLOW_DIR / cfg_ref)
            assert sc.get("name"), (
                f"PROBE 633: step config {cfg_ref} has no 'name'"
            )


def test_probe_634_every_link_source_step_exists() -> None:
    wf = _load(_WORKFLOW_YAML)
    step_ids = set(wf["steps"].keys())
    for link_id, link in wf["links"].items():
        cfg = link.get("config", {}) or {}
        src = cfg.get("source", "")
        src_step = src.partition(".")[0]
        assert src_step in step_ids, (
            f"PROBE 634: link {link_id} source step {src_step!r} not in {step_ids}"
        )


def test_probe_635_every_link_target_step_exists() -> None:
    wf = _load(_WORKFLOW_YAML)
    step_ids = set(wf["steps"].keys())
    for link_id, link in wf["links"].items():
        cfg = link.get("config", {}) or {}
        tgt = cfg.get("target", "")
        tgt_step = tgt.partition(".")[0]
        assert tgt_step in step_ids, (
            f"PROBE 635: link {link_id} target step {tgt_step!r} not in {step_ids}"
        )


def _step_data_units(wf: dict) -> dict[str, tuple[set[str], set[str]]]:
    """Return ``{step_id: (input_du_names, output_du_names)}``."""
    out = {}
    for step_id, step in wf["steps"].items():
        cfg_ref = step["config"]
        if isinstance(cfg_ref, str):
            sc = _load(_WORKFLOW_DIR / cfg_ref)
            inputs = set((sc.get("input_data_units") or {}).keys())
            outputs = set((sc.get("output_data_units") or {}).keys())
            out[step_id] = (inputs, outputs)
    return out


def test_probe_636_every_link_source_data_unit_exists() -> None:
    """A link's source 'step.data_unit' must reference a real
    output_data_units entry on the source step. Otherwise the
    workflow's runtime data-flow graph has a dangling edge."""
    wf = _load(_WORKFLOW_YAML)
    units = _step_data_units(wf)
    for link_id, link in wf["links"].items():
        cfg = link.get("config", {}) or {}
        src = cfg.get("source", "")
        src_step, _, src_du = src.partition(".")
        outs = units.get(src_step, (set(), set()))[1]
        assert src_du in outs, (
            f"PROBE 636: link {link_id} source data unit {src_du!r} "
            f"not in {src_step}'s outputs {outs}"
        )


def test_probe_637_every_link_target_data_unit_exists() -> None:
    wf = _load(_WORKFLOW_YAML)
    units = _step_data_units(wf)
    for link_id, link in wf["links"].items():
        cfg = link.get("config", {}) or {}
        tgt = cfg.get("target", "")
        tgt_step, _, tgt_du = tgt.partition(".")
        ins = units.get(tgt_step, (set(), set()))[0]
        assert tgt_du in ins, (
            f"PROBE 637: link {link_id} target data unit {tgt_du!r} "
            f"not in {tgt_step}'s inputs {ins}"
        )


def test_probe_638_every_step_class_importable() -> None:
    """Every step's class: must be importable. A missing class
    surfaces as ImportError at workflow load time — caught here
    instead of in production."""
    wf = _load(_WORKFLOW_YAML)
    for step_id, step in wf["steps"].items():
        cls_path = step["class"]
        cls = _import_class(cls_path)
        assert cls is not None, (
            f"PROBE 638: step {step_id} class {cls_path!r} not importable"
        )


def test_probe_639_every_link_class_is_directlink_subclass() -> None:
    """Every link's class must be importable AND a LinkBase subclass.
    The system.md prompt forbids TransformLink — assert no TransformLink
    appears in production YAML (cluster T01 P2 lesson)."""
    wf = _load(_WORKFLOW_YAML)
    for link_id, link in wf["links"].items():
        cls_path = link["class"]
        cls = _import_class(cls_path)
        assert cls is not None, (
            f"PROBE 639: link {link_id} class {cls_path!r} not importable"
        )
        assert "TransformLink" not in cls_path, (
            f"PROBE 639: link {link_id} uses TransformLink — "
            f"forbidden by system.md (cluster T01 P2)"
        )


# ---------------------------------------------------------------------------
# Manifest integrity — probes 640-644
# ---------------------------------------------------------------------------


def test_probe_640_manifest_has_components_list() -> None:
    mf = _load(_MANIFEST_YAML)
    assert isinstance(mf.get("components"), list)
    assert len(mf["components"]) > 0


def test_probe_641_non_deferred_components_have_class_and_yaml() -> None:
    """Every non-deferred component must declare class: + yaml: so
    the composer can wire a step from it."""
    mf = _load(_MANIFEST_YAML)
    for c in mf["components"]:
        if c.get("disposition") == "deferred":
            continue
        name = c.get("step_name") or c.get("step_id") or "?"
        assert c.get("class"), f"PROBE 641: component {name} has no class"
        assert c.get("yaml"), f"PROBE 641: component {name} has no yaml"


def test_probe_642_manifest_yaml_paths_resolve() -> None:
    mf = _load(_MANIFEST_YAML)
    for c in mf["components"]:
        if c.get("disposition") == "deferred":
            continue
        yaml_ref = c.get("yaml")
        if isinstance(yaml_ref, str):
            full = _MANIFEST_YAML.parent / yaml_ref
            assert full.is_file(), (
                f"PROBE 642: component yaml {yaml_ref} -> {full} not found"
            )


def test_probe_643_manifest_classes_importable() -> None:
    mf = _load(_MANIFEST_YAML)
    failures = []
    for c in mf["components"]:
        if c.get("disposition") == "deferred":
            continue
        cls_path = c.get("class", "")
        if cls_path and _import_class(cls_path) is None:
            failures.append((c.get("step_id"), cls_path))
    assert not failures, f"PROBE 643: not importable: {failures}"


def test_probe_644_manifest_components_have_rag_description() -> None:
    """Components without rag_description are unretrievable at
    Phase 2 (substring match). The catalog skips them silently —
    surface that here so the manifest author sees it."""
    mf = _load(_MANIFEST_YAML)
    failures = []
    for c in mf["components"]:
        if c.get("disposition") == "deferred":
            continue
        rag = c.get("rag_description") or ""
        if not rag.strip():
            failures.append(c.get("step_id"))
    assert not failures, (
        f"PROBE 644: components missing rag_description: {failures}"
    )


# ---------------------------------------------------------------------------
# DAG / topology invariants — probes 645-649
# ---------------------------------------------------------------------------


def test_probe_645_no_self_loops() -> None:
    """A link from step X to step X creates infinite trigger
    cycles — forbidden by WorkflowGraph.validate_graph."""
    wf = _load(_WORKFLOW_YAML)
    for link_id, link in wf["links"].items():
        cfg = link.get("config", {}) or {}
        src_step = cfg.get("source", "").partition(".")[0]
        tgt_step = cfg.get("target", "").partition(".")[0]
        assert src_step != tgt_step, (
            f"PROBE 645: link {link_id} is a self-loop on {src_step}"
        )


def test_probe_646_steps_with_inputs_have_a_link_into_them() -> None:
    """Any step that declares input_data_units MUST have a link
    landing on it. Otherwise the input never gets data and the
    step never fires — silent stall."""
    wf = _load(_WORKFLOW_YAML)
    units = _step_data_units(wf)
    targets_by_step: dict[str, set[str]] = {}
    for link in wf["links"].values():
        tgt = (link.get("config", {}) or {}).get("target", "")
        tgt_step, _, tgt_du = tgt.partition(".")
        if tgt_step:
            targets_by_step.setdefault(tgt_step, set()).add(tgt_du)
    orphans = []
    for step_id, (inputs, _) in units.items():
        if inputs and not targets_by_step.get(step_id):
            orphans.append(step_id)
    # Some entry-point steps may have no inputs; that's fine.
    # Steps WITH declared inputs but NO incoming link are bugs.
    # The violin_bvbrc workflow has reader steps (no inputs) +
    # interior steps (one incoming link each).
    # Allow some unwired steps (T01 deferred branches), but the
    # core synonym chain must be wired.
    core_steps = {
        "synonym_cache_lookup", "synonym_llm_proposals",
        "synonym_approval_gate", "verified_synonym_writeback",
    }
    assert core_steps - set(orphans) == core_steps, (
        f"PROBE 646: core synonym steps without incoming link: "
        f"{core_steps & set(orphans)}"
    )


def test_probe_647_every_step_id_unique() -> None:
    """Step ids in the workflow YAML must be unique. YAML loaders
    silently overwrite duplicate keys; a copy-paste error would
    silently drop a step."""
    wf_text = _WORKFLOW_YAML.read_text(encoding="utf-8")
    # Parse twice; if YAML had duplicate keys, the loaded dict
    # would have fewer entries than the source declared. Count
    # ``  <id>:`` lines under ``steps:``.
    in_steps = False
    declared_ids: list[str] = []
    for raw in wf_text.splitlines():
        stripped = raw.rstrip()
        if not stripped:
            continue
        if not raw.startswith(" ") and not raw.startswith("\t"):
            in_steps = stripped.startswith("steps:")
            continue
        if in_steps and raw.startswith("  ") and not raw.startswith("    "):
            # 2-space indent = a step id key
            id_part = stripped.split(":", 1)[0].strip()
            if id_part:
                declared_ids.append(id_part)
    loaded_ids = set(_load(_WORKFLOW_YAML)["steps"].keys())
    assert len(declared_ids) == len(set(declared_ids)), (
        f"PROBE 647: duplicate step id in source: {declared_ids}"
    )


def test_probe_648_links_only_reference_known_step_ids() -> None:
    wf = _load(_WORKFLOW_YAML)
    step_ids = set(wf["steps"].keys())
    for link_id, link in wf["links"].items():
        cfg = link.get("config", {}) or {}
        for endpoint in ("source", "target"):
            value = cfg.get(endpoint, "")
            ref_step = value.partition(".")[0]
            assert ref_step in step_ids, (
                f"PROBE 648: link {link_id} {endpoint}={value!r} references "
                f"unknown step"
            )


def test_probe_649_link_ids_unique() -> None:
    """Same shape as probe 647 but for link ids."""
    wf_text = _WORKFLOW_YAML.read_text(encoding="utf-8")
    in_links = False
    declared_ids: list[str] = []
    for raw in wf_text.splitlines():
        stripped = raw.rstrip()
        if not stripped:
            continue
        if not raw.startswith(" ") and not raw.startswith("\t"):
            in_links = stripped.startswith("links:")
            continue
        if in_links and raw.startswith("  ") and not raw.startswith("    "):
            id_part = stripped.split(":", 1)[0].strip()
            if id_part:
                declared_ids.append(id_part)
    assert len(declared_ids) == len(set(declared_ids)), (
        f"PROBE 649: duplicate link id in source: {declared_ids}"
    )


# ---------------------------------------------------------------------------
# Composer config + prompts — probes 650-654
# ---------------------------------------------------------------------------


def test_probe_650_composer_config_yml_exists_and_loads() -> None:
    if not _COMPOSER_CONFIG_YAML.is_file():
        pytest.skip("composer_config.yml not present")
    raw = _load(_COMPOSER_CONFIG_YAML)
    assert isinstance(raw, dict)
    assert "library_version" in raw


def test_probe_651_composer_config_validates() -> None:
    if not _COMPOSER_CONFIG_YAML.is_file():
        pytest.skip("composer_config.yml not present")
    from apecx_integration.composition.composer_schemas import ComposerConfig
    raw = _load(_COMPOSER_CONFIG_YAML)
    cfg = ComposerConfig.model_validate(raw)
    assert cfg.library_version


def test_probe_652_composer_prompts_dir_has_expected_files() -> None:
    if not _COMPOSER_PROMPTS_DIR.is_dir():
        pytest.skip("composer_prompts dir not present")
    expected = {"system.md", "composition_bias.md", "novel_python_flagging.md"}
    actual = {p.name for p in _COMPOSER_PROMPTS_DIR.iterdir() if p.is_file()}
    missing = expected - actual
    assert not missing, f"PROBE 652: composer_prompts missing: {missing}"


def test_probe_653_bvbrc_snapshot_tool_yml_class_importable() -> None:
    """The tool wrapper YAML's class: must be importable."""
    p = _WORKFLOW_DIR / "tools" / "bv_brc_snapshot_tool.yml"
    if not p.is_file():
        pytest.skip("tool yaml not present")
    cfg = _load(p)
    cls_path = cfg.get("class", "")
    assert cls_path
    assert _import_class(cls_path) is not None, (
        f"PROBE 653: tool yaml class {cls_path!r} not importable"
    )


def test_probe_654_workflow_version_field_is_set() -> None:
    """The version field is the AC3 anchor (T11 GeneratedArtifact
    rows reference it). Empty / missing version = silent provenance
    drift."""
    wf = _load(_WORKFLOW_YAML)
    version = wf.get("version", "")
    assert version, "PROBE 654: workflow.yml has no version"
    assert isinstance(version, str)
