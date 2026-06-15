"""Probe batch 42 — adversarial probes against the composer's RAG
path AND file reader edge cases.

Streak before this batch: 49/300 post-AQ.
Probe naming: 1105–1129.

Distinct probes only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from apecx_integration.composition.composer_schemas import ComposerConfig

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSER_CONFIG = REPO_ROOT / "src" / "apecx_integration" / "composition" / "composer_config.yml"
# violin_bvbrc retired 2026-06-15; these generic workflow-YAML hygiene
# probes now validate the surviving rag_e2e_synthesis workflow.
WORKFLOW_DIR = (
    REPO_ROOT / "src" / "apecx_integration" / "composition" / "workflows" / "rag_e2e_synthesis"
)
WORKFLOW_YAML_NAME = "rag_e2e_synthesis_workflow.yml"


# --------------------------------------------------------------------------- #
# Probes 1105–1129
# --------------------------------------------------------------------------- #


def test_probe_1105_composer_config_loads_via_yaml_with_extra_forbid():
    """The composer YAML must validate cleanly under
    ``ComposerConfig(extra='forbid')`` (workspace rule audit pass)."""
    import yaml

    raw = yaml.safe_load(COMPOSER_CONFIG.read_text())
    cfg = ComposerConfig.model_validate(raw)
    assert cfg.library_version


def test_probe_1106_composer_config_typo_field_rejected():
    """Pin the audit-pass result: ``ComposerConfig`` rejects unknown
    keys (probe 955 found this, probe-batch-36 fix applied; this
    probe is the regression guard)."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match=r"[Ee]xtra"):
        ComposerConfig.model_validate(
            {
                "library_version": "0.1.0",
                "prompt_dir": "/tmp",
                "componnet_catalog_paths": [],  # typo
            }
        )


def test_probe_1107_composer_config_max_tokens_negative_rejected():
    """ComposerConfig.max_tokens has ge=1 — probes pin the constraint."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ComposerConfig.model_validate(
            {
                "library_version": "0.1.0",
                "prompt_dir": "/tmp",
                "max_tokens": -1,
            }
        )


def test_probe_1108_composer_config_temperature_above_2_rejected():
    """temperature has le=2.0 — verify upper bound."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ComposerConfig.model_validate(
            {
                "library_version": "0.1.0",
                "prompt_dir": "/tmp",
                "temperature": 2.5,
            }
        )


def test_probe_1109_composer_config_retrieval_k_zero_rejected():
    """retrieval_k has ge=1 — k=0 means retrieve nothing, defensible
    bug shape that the schema correctly rejects."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ComposerConfig.model_validate(
            {
                "library_version": "0.1.0",
                "prompt_dir": "/tmp",
                "retrieval_k": 0,
            }
        )


def test_probe_1110_composer_config_max_retries_zero_accepted():
    """max_retries has ge=0 — zero means dev mode (fail-fast on
    transient LLM 5xx). Operator explicit choice; pin acceptance."""
    cfg = ComposerConfig.model_validate(
        {
            "library_version": "0.1.0",
            "prompt_dir": "/tmp",
            "max_retries": 0,
        }
    )
    assert cfg.max_retries == 0


def test_probe_1111_composer_config_path_fields_accept_pathlike(tmp_path):
    """Path fields (prompt_dir, sandbox_whitelist_path, rag_index_dir)
    accept str AND Path. Verify both shapes load."""
    cfg_str = ComposerConfig.model_validate(
        {
            "library_version": "0.1.0",
            "prompt_dir": str(tmp_path),
        }
    )
    cfg_path = ComposerConfig.model_validate(
        {
            "library_version": "0.1.0",
            "prompt_dir": tmp_path,
        }
    )
    # Both produce a Path.
    assert isinstance(cfg_str.prompt_dir, Path)
    assert isinstance(cfg_path.prompt_dir, Path)


def test_probe_1112_composer_config_rag_index_dir_optional():
    """rag_index_dir is Optional[Path]; the default is None and the
    composer falls back to linear-scan ComponentCatalog."""
    cfg = ComposerConfig.model_validate(
        {
            "library_version": "0.1.0",
            "prompt_dir": "/tmp",
        }
    )
    assert cfg.rag_index_dir is None


def test_probe_1113_composer_config_component_catalog_paths_default_empty():
    """``component_catalog_paths`` default is []. A non-list value
    must be rejected by Pydantic."""
    from pydantic import ValidationError

    cfg = ComposerConfig.model_validate(
        {
            "library_version": "0.1.0",
            "prompt_dir": "/tmp",
        }
    )
    assert cfg.component_catalog_paths == []
    with pytest.raises(ValidationError):
        ComposerConfig.model_validate(
            {
                "library_version": "0.1.0",
                "prompt_dir": "/tmp",
                "component_catalog_paths": "not_a_list",
            }
        )


def test_probe_1114_violin_workflow_yaml_includes_no_dangling_step_yaml_paths():
    """Every step's ``config: 'steps/X.yml'`` path must point at an
    existing file. A future commit deleting a step YAML but leaving
    its registration would silently fail at workflow boot. Pin."""
    import yaml

    raw = yaml.safe_load((WORKFLOW_DIR / WORKFLOW_YAML_NAME).read_text())
    steps = raw.get("steps", {})
    for step_id, step_def in steps.items():
        cfg_path = step_def.get("config")
        if not cfg_path:
            continue
        full = WORKFLOW_DIR / cfg_path
        assert full.is_file(), f"step {step_id} references missing config: {cfg_path}"


def test_probe_1115_step_yaml_files_all_have_class_field():
    """Every wrapper YAML in workflows/violin_bvbrc/steps/ must
    declare a ``class:`` field. Missing class would silently fail
    at from_config with a confusing pydantic error."""
    import yaml

    steps_dir = WORKFLOW_DIR / "steps"
    for step_yaml in steps_dir.glob("*.yml"):
        raw = yaml.safe_load(step_yaml.read_text())
        assert "class" in raw, f"{step_yaml.name} missing 'class' field"


def test_probe_1116_step_yaml_class_paths_resolve_to_real_classes():
    """Every ``class:`` field in step YAMLs must resolve to an
    importable class. A typo'd path would silently fail at
    workflow load. Verify via importlib."""
    import importlib

    import yaml

    steps_dir = WORKFLOW_DIR / "steps"
    for step_yaml in steps_dir.glob("*.yml"):
        raw = yaml.safe_load(step_yaml.read_text())
        cls_path = raw.get("class")
        if not cls_path:
            continue
        module_path, _, cls_name = cls_path.rpartition(".")
        try:
            mod = importlib.import_module(module_path)
            assert hasattr(mod, cls_name), (
                f"{step_yaml.name}: class {cls_name!r} missing from {module_path!r}"
            )
        except ImportError as e:
            pytest.fail(f"{step_yaml.name}: cannot import {module_path}: {e}")


def test_probe_1117_step_yaml_input_data_units_have_class_paths():
    """Every step YAML's input_data_units entry must declare a class
    (DataUnitMemory / DataUnitFile / etc.). Missing class would
    silently default to the framework's choice."""
    import yaml

    steps_dir = WORKFLOW_DIR / "steps"
    for step_yaml in steps_dir.glob("*.yml"):
        raw = yaml.safe_load(step_yaml.read_text())
        inputs = raw.get("input_data_units") or {}
        for du_id, du_def in inputs.items():
            assert "class" in du_def, f"{step_yaml.name}: input_data_units[{du_id!r}] missing class"


def test_probe_1118_step_yaml_triggers_have_class_paths():
    """Every trigger declaration must carry a class path."""
    import yaml

    steps_dir = WORKFLOW_DIR / "steps"
    for step_yaml in steps_dir.glob("*.yml"):
        raw = yaml.safe_load(step_yaml.read_text())
        triggers = raw.get("triggers") or []
        for i, trig_def in enumerate(triggers):
            assert "class" in trig_def, f"{step_yaml.name}: triggers[{i}] missing class"


def test_probe_1119_step_yaml_no_inline_prompt_strings():
    """Per AC6 (composer prompts spec): NO inline prompt strings in
    any wrapper YAML. Operators tune via prompt_dir override; an
    inline prompt would silently freeze the prompt at YAML write."""
    import yaml

    steps_dir = WORKFLOW_DIR / "steps"
    for step_yaml in steps_dir.glob("*.yml"):
        raw = yaml.safe_load(step_yaml.read_text())
        # Look for any field named 'prompt' or 'system_prompt' at
        # top level (inline string).
        for key in ("prompt", "system_prompt"):
            v = raw.get(key)
            assert not (isinstance(v, str) and len(v) > 50), (
                f"{step_yaml.name}: inline {key!r} string detected "
                f"(should reference a file via prompt_dir / config)"
            )


def test_probe_1120_synthesis_config_yml_in_rag_synthesis_dir_loads():
    """The bundled synthesis_config.yml must validate cleanly under
    SynthesisConfig (extra='forbid'). Probe 1052 covered the
    schema/yaml comparison; this is the existence + load check."""
    import yaml

    from apecx_integration.agents.rag_synthesis import (
        DEFAULT_SYNTHESIS_CONFIG_PATH,
        SynthesisConfig,
    )

    assert DEFAULT_SYNTHESIS_CONFIG_PATH.is_file()
    raw = yaml.safe_load(DEFAULT_SYNTHESIS_CONFIG_PATH.read_text())
    SynthesisConfig.model_validate(raw)  # raises if invalid


def test_probe_1121_synthesis_config_yml_no_extra_keys():
    """The bundled synthesis_config.yml has only documented keys.
    A future contributor adding a typo + a working key together
    would have only the typo caught (the working key would still
    apply). Pin the YAML's key set to be a subset of the schema's
    fields."""
    import yaml

    from apecx_integration.agents.rag_synthesis import SynthesisConfig
    from apecx_integration.agents.rag_synthesis.synthesizer import (
        DEFAULT_SYNTHESIS_CONFIG_PATH,
    )

    raw = yaml.safe_load(DEFAULT_SYNTHESIS_CONFIG_PATH.read_text())
    schema_fields = set(SynthesisConfig.model_fields.keys())
    yaml_keys = set(raw.keys())
    extra = yaml_keys - schema_fields
    assert not extra, f"unknown keys in default YAML: {extra}"


def test_probe_1122_default_synthesis_config_path_is_inside_package():
    """The bundled config must live inside the package — not at the
    repo root or in /tmp. A path outside the package would break
    pip-install distribution."""
    from apecx_integration.agents.rag_synthesis.synthesizer import (
        DEFAULT_SYNTHESIS_CONFIG_PATH,
    )

    pkg_dir = Path(__import__("apecx_integration.agents.rag_synthesis").__file__).parent
    assert (
        pkg_dir in DEFAULT_SYNTHESIS_CONFIG_PATH.parents
        or DEFAULT_SYNTHESIS_CONFIG_PATH.is_relative_to(pkg_dir.parent)
    ), (
        f"DEFAULT_SYNTHESIS_CONFIG_PATH={DEFAULT_SYNTHESIS_CONFIG_PATH} "
        f"not under package dir {pkg_dir}"
    )


def test_probe_1123_workflow_yaml_step_ids_unique():
    """Step IDs must be unique within the workflow YAML. YAML allows
    duplicate keys (last wins), which would silently drop a step."""
    text = (WORKFLOW_DIR / WORKFLOW_YAML_NAME).read_text()
    # Crude check: step IDs are at 2-space indent under 'steps:'.
    # Count occurrences of each step id via a manual scan — pyyaml drops
    # duplicate keys silently (last wins), so a parsed dict can't detect them.
    import re

    candidate_ids = re.findall(r"^  ([a-zA-Z_][a-zA-Z0-9_]*):", text, flags=re.M)
    duplicates = [c for c in candidate_ids if candidate_ids.count(c) > 1]
    assert not duplicates, f"duplicate step ids in workflow YAML: {set(duplicates)!r}"


def test_probe_1124_step_yaml_rag_synthesis_input_data_unit_name_matches_class_attr():
    """The step expects ``input_data: {query, rag_chunks, ...}``;
    the wrapper YAML wires a SINGLE input_data_unit named
    ``synthesis_input``. Probe: the input data unit's name in the
    wrapper YAML matches what the framework uses to deliver data
    to the step's process(). A name mismatch would silently never
    fire the trigger."""
    import yaml

    raw = yaml.safe_load((WORKFLOW_DIR / "steps" / "rag_synthesis.yml").read_text())
    inputs = raw.get("input_data_units", {})
    assert "synthesis_input" in inputs
    triggers = raw.get("triggers", [])
    assert any(t.get("data_unit") == "synthesis_input" for t in triggers), (
        "no trigger references synthesis_input"
    )


def test_probe_1125_workflow_yaml_no_step_with_blank_class_path():
    """Empty class string would crash from_config; pin all classes
    are non-empty strings."""
    import yaml

    raw = yaml.safe_load((WORKFLOW_DIR / WORKFLOW_YAML_NAME).read_text())
    for step_id, step_def in raw.get("steps", {}).items():
        cls = step_def.get("class")
        assert isinstance(cls, str) and cls, f"step {step_id!r} has empty/missing class path"


def test_probe_1126_synthesis_yaml_has_pattern_excluding_whitespace():
    """Post-fix-1066 the bundled YAML's patterns exclude whitespace
    AND open-bracket. If a future commit accidentally reverts the
    YAML's patterns to the broad ``[^\\]]+`` form, this probe
    catches it."""
    import yaml

    from apecx_integration.agents.rag_synthesis.synthesizer import (
        DEFAULT_SYNTHESIS_CONFIG_PATH,
    )

    raw = yaml.safe_load(DEFAULT_SYNTHESIS_CONFIG_PATH.read_text())
    patterns = raw.get("citation_marker_patterns", [])
    # Pattern strings that have an inner class must use the tightened
    # form (exclude whitespace + brackets). Skip the RAG-chunk-#
    # pattern (digit-bounded, never had the bug).
    for pat in patterns:
        if "RAG chunk" in pat:
            continue
        # The new form has ``\\s\\[`` inside the negation class.
        assert "\\s" in pat or "\\\\s" in pat, (
            f"pattern {pat!r} does not exclude whitespace -- "
            f"regression to bug 1066's vulnerable form"
        )


def test_probe_1127_composer_config_max_retries_default_zero_means_dev_mode():
    """Per the docstring: max_retries default is 0 to give dev-loop
    fast feedback. Operators MUST override for prod (no default
    retry on transient 5xx). Pin the default so a future "safer"
    change to 3 doesn't silently change dev behavior."""
    cfg = ComposerConfig.model_validate(
        {
            "library_version": "0.1.0",
            "prompt_dir": "/tmp",
        }
    )
    assert cfg.max_retries == 0, "default max_retries changed; review the prod-vs-dev tradeoff"


def test_probe_1128_workflow_yaml_links_each_have_unique_id():
    """Link IDs (the keys under links:) must be unique. YAML duplicate
    keys silently last-wins, which would silently drop a link."""
    import re

    text = (WORKFLOW_DIR / WORKFLOW_YAML_NAME).read_text()
    # Find link IDs at 2-space indent under 'links:'.
    in_links = False
    link_ids = []
    for line in text.splitlines():
        if line.startswith("links:"):
            in_links = True
            continue
        if in_links:
            m = re.match(r"^  ([a-zA-Z_][a-zA-Z0-9_]*):", line)
            if m:
                link_ids.append(m.group(1))
    duplicates = [lid for lid in link_ids if link_ids.count(lid) > 1]
    assert not duplicates, f"duplicate link ids: {set(duplicates)!r}"


def test_probe_1129_step_yaml_rag_synthesis_carries_no_inline_synthesis_config():
    """The wrapper YAML's ``synthesis_config_path`` is commented out
    (default = bundled config). A future change adding an inline
    ``synthesis_config:`` block would silently bypass the override
    contract — the step expects a path string, not an inline dict."""
    import yaml

    raw = yaml.safe_load((WORKFLOW_DIR / "steps" / "rag_synthesis.yml").read_text())
    # An inline dict block would be a non-string value.
    val = raw.get("synthesis_config_path")
    if val is not None:
        assert isinstance(val, str), (
            f"synthesis_config_path must be a path string, got {type(val).__name__}"
        )
