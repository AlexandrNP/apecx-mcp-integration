"""Probe batch 11 — workflow YAML integrity + manifest accuracy.

Probes 251-275. Each is one distinct probe. Focus: do the
workflow YAMLs and step wrapper YAMLs in the violin_bvbrc tree
actually meet the framework's expectations? Are class paths in
the manifest real?
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml


pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
WF_DIR = (
    REPO_ROOT
    / "src"
    / "apecx_integration"
    / "composition"
    / "workflows"
    / "violin_bvbrc"
)
STEPS_DIR = WF_DIR / "steps"


# --- Probe 251: violin_bvbrc workflow YAML loads ---


def test_probe_251_violin_workflow_yaml_loads() -> None:
    p = WF_DIR / "violin_bvbrc_workflow.yml"
    if not p.is_file():
        pytest.skip()
    parsed = yaml.safe_load(p.read_text())
    assert isinstance(parsed, dict)


# --- Probe 252: workflow has top-level keys (name, description, steps, links) ---


def test_probe_252_workflow_top_level_keys() -> None:
    p = WF_DIR / "violin_bvbrc_workflow.yml"
    if not p.is_file():
        pytest.skip()
    parsed = yaml.safe_load(p.read_text())
    assert "name" in parsed
    assert "steps" in parsed


# --- Probe 253: every step in main workflow has class + config ---


def test_probe_253_every_step_has_class_and_config() -> None:
    p = WF_DIR / "violin_bvbrc_workflow.yml"
    if not p.is_file():
        pytest.skip()
    parsed = yaml.safe_load(p.read_text())
    steps = parsed.get("steps", {})
    for step_id, step_def in steps.items():
        assert "class" in step_def, (
            f"PROBE 253: step {step_id} has no 'class' field"
        )
        assert "config" in step_def, (
            f"PROBE 253: step {step_id} has no 'config' field"
        )


# --- Probe 254: every step's class path is importable ---


def test_probe_254_every_step_class_importable() -> None:
    p = WF_DIR / "violin_bvbrc_workflow.yml"
    if not p.is_file():
        pytest.skip()
    try:
        import nanobrain  # noqa: F401
    except ImportError:
        pytest.skip("nanobrain not importable")
    parsed = yaml.safe_load(p.read_text())
    steps = parsed.get("steps", {})
    for step_id, step_def in steps.items():
        cls = step_def["class"]
        module_path, _, class_name = cls.rpartition(".")
        try:
            module = importlib.import_module(module_path)
            assert hasattr(module, class_name), (
                f"PROBE 254 BUG: step {step_id} class {cls} module exists but "
                f"has no attribute {class_name}"
            )
        except ImportError as e:
            pytest.fail(
                f"PROBE 254 BUG: step {step_id} class {cls} module not "
                f"importable: {e}"
            )


# --- Probe 255: every step's config (path) resolves to a file ---


def test_probe_255_step_config_paths_resolve() -> None:
    p = WF_DIR / "violin_bvbrc_workflow.yml"
    if not p.is_file():
        pytest.skip()
    parsed = yaml.safe_load(p.read_text())
    steps = parsed.get("steps", {})
    for step_id, step_def in steps.items():
        config = step_def["config"]
        if not isinstance(config, str):
            continue  # inline configs are out of scope here
        # Path is relative to the workflow YAML's directory
        cp = WF_DIR / config
        assert cp.is_file(), (
            f"PROBE 255 BUG: step {step_id} config={config} -> "
            f"{cp} doesn't exist"
        )


# --- Probe 256: every step wrapper YAML loads ---


def test_probe_256_step_wrappers_load() -> None:
    if not STEPS_DIR.is_dir():
        pytest.skip()
    for f in STEPS_DIR.glob("*.yml"):
        try:
            yaml.safe_load(f.read_text())
        except yaml.YAMLError as e:
            pytest.fail(f"PROBE 256 BUG: {f.name} not parseable: {e}")


# --- Probe 257: every step wrapper has 'class' field ---


def test_probe_257_step_wrappers_have_class() -> None:
    if not STEPS_DIR.is_dir():
        pytest.skip()
    for f in STEPS_DIR.glob("*.yml"):
        parsed = yaml.safe_load(f.read_text())
        if not isinstance(parsed, dict):
            continue
        # Wrappers may be nested; the top level often has class
        # But some files are nested config — accept either.
        if "class" not in parsed:
            # Check for sub-mappings with class
            has_class = any(
                isinstance(v, dict) and "class" in v
                for v in parsed.values()
            )
            assert has_class or "name" in parsed, (
                f"PROBE 257: {f.name} top level missing class/name; "
                f"keys: {list(parsed.keys())}"
            )


# --- Probe 258: manifest.yml exists ---


def test_probe_258_manifest_exists() -> None:
    p = WF_DIR / "manifest.yml"
    if not p.is_file():
        pytest.skip("manifest.yml not present")
    parsed = yaml.safe_load(p.read_text())
    assert isinstance(parsed, dict) or isinstance(parsed, list)


# --- Probe 259: manifest entries have implementation_path ---


def test_probe_259_manifest_implementation_paths() -> None:
    p = WF_DIR / "manifest.yml"
    if not p.is_file():
        pytest.skip()
    parsed = yaml.safe_load(p.read_text())
    # The manifest may be a list or dict-of-components
    components = parsed if isinstance(parsed, list) else parsed.get("components", parsed)
    if isinstance(components, dict):
        components = list(components.values())
    for c in components if isinstance(components, list) else []:
        if isinstance(c, dict):
            # May have implementation_path or class
            assert "implementation_path" in c or "class" in c, (
                f"PROBE 259: manifest entry missing implementation_path: {c}"
            )


# --- Probe 260: every manifest implementation_path is importable ---


def test_probe_260_manifest_paths_importable() -> None:
    p = WF_DIR / "manifest.yml"
    if not p.is_file():
        pytest.skip()
    try:
        import nanobrain  # noqa
    except ImportError:
        pytest.skip()
    parsed = yaml.safe_load(p.read_text())
    components = parsed if isinstance(parsed, list) else parsed.get("components", parsed)
    if isinstance(components, dict):
        components = list(components.values())
    for c in components if isinstance(components, list) else []:
        if not isinstance(c, dict):
            continue
        path = c.get("implementation_path") or c.get("class")
        if not path:
            continue
        module_path, _, class_name = path.rpartition(".")
        try:
            module = importlib.import_module(module_path)
            assert hasattr(module, class_name), (
                f"PROBE 260 BUG: manifest path {path} class not found"
            )
        except ImportError as e:
            pytest.fail(f"PROBE 260 BUG: manifest path {path} not importable: {e}")


# --- Probe 261: every step wrapper that references a yaml field points at a real file ---


def test_probe_261_step_wrapper_yaml_refs_resolve() -> None:
    p = WF_DIR / "manifest.yml"
    if not p.is_file():
        pytest.skip()
    parsed = yaml.safe_load(p.read_text())
    components = parsed if isinstance(parsed, list) else parsed.get("components", parsed)
    if isinstance(components, dict):
        components = list(components.values())
    for c in components if isinstance(components, list) else []:
        if not isinstance(c, dict):
            continue
        ywl = c.get("yaml")
        if not ywl:
            continue
        cp = WF_DIR / ywl
        assert cp.is_file(), (
            f"PROBE 261 BUG: manifest 'yaml' field {ywl} -> {cp} doesn't exist"
        )


# --- Probe 262: workflow YAML has no inline config for library steps ---


def test_probe_262_no_inline_config_for_library_steps() -> None:
    """system.md mandates path-reference config for library
    components. Check the actual workflow doesn't violate."""
    p = WF_DIR / "violin_bvbrc_workflow.yml"
    if not p.is_file():
        pytest.skip()
    parsed = yaml.safe_load(p.read_text())
    steps = parsed.get("steps", {})
    for step_id, step_def in steps.items():
        config = step_def.get("config")
        # Library components should have str config (path); novel
        # components might have inline. We can't tell which is
        # which without the manifest, so just assert that string
        # configs are paths to actual files.
        if isinstance(config, str):
            cp = WF_DIR / config
            assert cp.is_file(), (
                f"PROBE 262: step {step_id} config={config!r} not a real file"
            )


# --- Probe 263: workflow YAML has no top-level data_units key ---


def test_probe_263_no_toplevel_data_units() -> None:
    """system.md forbids top-level data_units. Check the real
    workflow respects this."""
    p = WF_DIR / "violin_bvbrc_workflow.yml"
    if not p.is_file():
        pytest.skip()
    parsed = yaml.safe_load(p.read_text())
    assert "data_units" not in parsed


# --- Probe 264: workflow YAML has no top-level triggers key ---


def test_probe_264_no_toplevel_triggers() -> None:
    p = WF_DIR / "violin_bvbrc_workflow.yml"
    if not p.is_file():
        pytest.skip()
    parsed = yaml.safe_load(p.read_text())
    assert "triggers" not in parsed


# --- Probe 265: every link uses DirectLink class ---


def test_probe_265_links_use_directlink() -> None:
    p = WF_DIR / "violin_bvbrc_workflow.yml"
    if not p.is_file():
        pytest.skip()
    parsed = yaml.safe_load(p.read_text())
    links = parsed.get("links", {})
    for link_id, link_def in links.items():
        if isinstance(link_def, dict):
            cls = link_def.get("class", "")
            assert "DirectLink" in cls, (
                f"PROBE 265: link {link_id} class={cls} — only DirectLink "
                "allowed per system.md"
            )


# --- Probe 266: every link source/target points at a defined step ---


def test_probe_266_link_endpoints_resolve() -> None:
    p = WF_DIR / "violin_bvbrc_workflow.yml"
    if not p.is_file():
        pytest.skip()
    parsed = yaml.safe_load(p.read_text())
    steps = parsed.get("steps", {})
    step_ids = set(steps.keys())
    links = parsed.get("links", {})
    for link_id, link_def in links.items():
        if not isinstance(link_def, dict):
            continue
        cfg = link_def.get("config", {})
        for endpoint_key in ("source", "target"):
            ep = cfg.get(endpoint_key, "")
            if isinstance(ep, str) and "." in ep:
                step_id, _, _ = ep.partition(".")
                assert step_id in step_ids, (
                    f"PROBE 266: link {link_id} {endpoint_key}={ep!r} "
                    f"refers to unknown step {step_id}"
                )


# --- Probe 267: workflow doesn't link a step to itself ---


def test_probe_267_no_self_links() -> None:
    p = WF_DIR / "violin_bvbrc_workflow.yml"
    if not p.is_file():
        pytest.skip()
    parsed = yaml.safe_load(p.read_text())
    links = parsed.get("links", {})
    for link_id, link_def in links.items():
        if not isinstance(link_def, dict):
            continue
        cfg = link_def.get("config", {})
        src = cfg.get("source", "")
        tgt = cfg.get("target", "")
        src_step = src.split(".")[0] if isinstance(src, str) else ""
        tgt_step = tgt.split(".")[0] if isinstance(tgt, str) else ""
        if src_step and tgt_step:
            assert src_step != tgt_step, (
                f"PROBE 267: link {link_id} self-loops on step {src_step}"
            )


# --- Probe 268: every step wrapper YAML can be loaded as nanobrain Step.from_config ---


def test_probe_268_step_wrappers_loadable_via_step_from_config() -> None:
    """Smoke test: each step wrapper YAML is structurally what
    nanobrain expects (no schema validation here, just parse)."""
    if not STEPS_DIR.is_dir():
        pytest.skip()
    for f in STEPS_DIR.glob("*.yml"):
        parsed = yaml.safe_load(f.read_text())
        # Step wrappers should have either 'class' or be a config
        # mapping with named keys.
        assert isinstance(parsed, dict), (
            f"PROBE 268: {f.name} top-level not a dict"
        )


# --- Probe 269: no duplicate step names within a workflow ---


def test_probe_269_no_duplicate_step_names() -> None:
    """YAML mappings can't have duplicate keys; if they do,
    yaml.safe_load silently keeps the last one. Check by reading
    raw text and looking for duplicates."""
    p = WF_DIR / "violin_bvbrc_workflow.yml"
    if not p.is_file():
        pytest.skip()
    text = p.read_text()
    # Heuristic: under "steps:" extract step_id keys (lines that
    # are 2-space indented and end with `:`).
    in_steps = False
    seen: set[str] = set()
    for line in text.splitlines():
        if line.startswith("steps:"):
            in_steps = True
            continue
        if in_steps and line and not line.startswith(" ") and not line.startswith("\t"):
            # next top-level key
            in_steps = False
        if in_steps:
            stripped = line.strip()
            # 2-space indent + key:
            if (
                line.startswith("  ")
                and not line.startswith("    ")
                and stripped.endswith(":")
            ):
                key = stripped.rstrip(":").strip()
                assert key not in seen, (
                    f"PROBE 269: duplicate step name {key!r} in workflow YAML"
                )
                seen.add(key)


# --- Probe 270: workflow YAML uses LF line endings (not CRLF) ---


def test_probe_270_workflow_yaml_lf_only() -> None:
    p = WF_DIR / "violin_bvbrc_workflow.yml"
    if not p.is_file():
        pytest.skip()
    raw = p.read_bytes()
    assert b"\r\n" not in raw, "PROBE 270: workflow YAML has CRLF line endings"


# --- Probe 271: composer prompt files use LF ---


def test_probe_271_prompt_files_lf_only() -> None:
    pdir = REPO_ROOT / "src" / "apecx_integration" / "composition" / "composer_prompts"
    for f in pdir.glob("*.md"):
        raw = f.read_bytes()
        assert b"\r\n" not in raw, f"PROBE 271: {f.name} has CRLF endings"


# --- Probe 272: workflow YAML doesn't have trailing whitespace on lines ---


def test_probe_272_workflow_yaml_no_trailing_whitespace() -> None:
    p = WF_DIR / "violin_bvbrc_workflow.yml"
    if not p.is_file():
        pytest.skip()
    text = p.read_text()
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.endswith(" ") or line.endswith("\t"):
            # Trailing whitespace can break pinned-content hashes.
            # Worth flagging.
            pytest.fail(f"PROBE 272: workflow YAML line {lineno} has trailing whitespace")


# --- Probe 273: all step wrappers are well-named (lowercase + underscores) ---


def test_probe_273_step_wrapper_names() -> None:
    if not STEPS_DIR.is_dir():
        pytest.skip()
    import re
    for f in STEPS_DIR.glob("*.yml"):
        name = f.stem
        assert re.match(r"^[a-z][a-z0-9_]*$", name), (
            f"PROBE 273: step wrapper name {name!r} should be lowercase_underscore"
        )


# --- Probe 274: workflow file ends with newline ---


def test_probe_274_workflow_yaml_terminal_newline() -> None:
    p = WF_DIR / "violin_bvbrc_workflow.yml"
    if not p.is_file():
        pytest.skip()
    raw = p.read_bytes()
    assert raw.endswith(b"\n"), "PROBE 274: workflow YAML missing terminal newline"


# --- Probe 275: workflow's step count is non-zero ---


def test_probe_275_workflow_has_steps() -> None:
    p = WF_DIR / "violin_bvbrc_workflow.yml"
    if not p.is_file():
        pytest.skip()
    parsed = yaml.safe_load(p.read_text())
    steps = parsed.get("steps", {})
    assert len(steps) > 0, "PROBE 275: workflow has no steps"
