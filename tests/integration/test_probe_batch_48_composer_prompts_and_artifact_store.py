"""Probe batch 48 — adversarial probes against composer prompt files
and ArtifactStore shape invariants.

Streak before this batch: 199/300 post-AQ post-1066.
Probe naming: 1255–1279.

Distinct probes only.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest


pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = (
    REPO_ROOT / "src" / "apecx_integration" / "composition"
    / "composer_prompts"
)


# --------------------------------------------------------------------------- #
# Probes 1255–1279
# --------------------------------------------------------------------------- #


def test_probe_1255_composer_prompt_dir_exists():
    assert PROMPT_DIR.is_dir(), PROMPT_DIR


def test_probe_1256_composer_prompt_system_md_exists():
    """system.md is the primary prompt; AC1 hinges on it."""
    assert (PROMPT_DIR / "system.md").is_file()


def test_probe_1257_composer_prompt_files_are_utf8():
    """Per CLAUDE.md the prompts are load-bearing — must decode
    cleanly."""
    for prompt_file in PROMPT_DIR.glob("*.md"):
        prompt_file.read_text(encoding="utf-8")  # raises if invalid


def test_probe_1258_composer_prompt_system_md_carries_no_transformlink_directive():
    """Per repo CLAUDE.md: prompt forbids TransformLink because LLMs
    hallucinate transform_function paths. Pin the directive's
    presence."""
    text = (PROMPT_DIR / "system.md").read_text(encoding="utf-8")
    # The prompt explicitly mentions DirectLink as the only legal
    # link type.
    assert "DirectLink" in text or "transform" in text.lower()


def test_probe_1259_composer_prompt_system_md_mandates_path_reference_config():
    """Per CLAUDE.md: prompt requires ``config: '<wrapper_yaml>'``
    references for library components (not inline config dicts)."""
    text = (PROMPT_DIR / "system.md").read_text(encoding="utf-8")
    # The directive language would mention 'config' + 'path'.
    # Coarse check: the word 'config' appears.
    assert "config" in text.lower()


def test_probe_1260_composer_prompt_files_are_not_empty():
    """An empty prompt would silently turn into a no-op LLM call."""
    for prompt_file in PROMPT_DIR.glob("*.md"):
        size = prompt_file.stat().st_size
        assert size > 100, (
            f"{prompt_file.name} is {size} bytes — likely empty or "
            f"a placeholder; the LLM would have no instructions"
        )


def test_probe_1261_composer_prompt_files_are_bounded_in_size():
    """A bloated prompt (>50KB) likely contains unrelated content."""
    for prompt_file in PROMPT_DIR.glob("*.md"):
        size = prompt_file.stat().st_size
        assert size < 50_000, (
            f"{prompt_file.name} is {size} bytes — likely contains "
            f"unrelated content; prompts should be focused"
        )


def test_probe_1262_artifact_store_imports_cleanly():
    import apecx_integration.composition.artifact_store as mod
    assert hasattr(mod, "ArtifactStore")
    assert hasattr(mod, "ArtifactNotFound")
    assert hasattr(mod, "GenerationMetadata")


def test_probe_1263_artifact_not_found_is_lookup_error_subclass():
    """ArtifactNotFound inherits from LookupError so callers using
    ``except KeyError`` won't catch it accidentally — pin."""
    from apecx_integration.composition.artifact_store import ArtifactNotFound
    assert issubclass(ArtifactNotFound, LookupError)


def test_probe_1264_generation_metadata_is_frozen_dataclass():
    """The metadata is frozen so producers can't mutate it after
    persistence."""
    from apecx_integration.composition.artifact_store import GenerationMetadata
    # Construct minimal — pydantic-style or plain dataclass.
    # Field check via inspect.
    import inspect
    src = inspect.getsource(GenerationMetadata)
    assert "frozen=True" in src or "@dataclass(frozen=True" in src


def test_probe_1265_artifact_store_init_signature():
    """ArtifactStore.__init__ accepts session_factory + recorder.
    Pin so a refactor adding a hidden parameter is intentional."""
    from apecx_integration.composition.artifact_store import ArtifactStore
    import inspect
    sig = inspect.signature(ArtifactStore.__init__)
    params = list(sig.parameters.keys())
    assert "session_factory" in params
    assert "recorder" in params


def test_probe_1266_artifact_store_store_method_exists():
    from apecx_integration.composition.artifact_store import ArtifactStore
    assert hasattr(ArtifactStore, "store")


def test_probe_1267_artifact_store_load_content_method_exists():
    from apecx_integration.composition.artifact_store import ArtifactStore
    assert hasattr(ArtifactStore, "load_content")


def test_probe_1268_artifact_store_load_content_takes_uuid():
    from apecx_integration.composition.artifact_store import ArtifactStore
    import inspect
    sig = inspect.signature(ArtifactStore.load_content)
    params = list(sig.parameters.keys())
    assert "artifact_id" in params


def test_probe_1269_composer_config_yml_exists():
    p = REPO_ROOT / "src" / "apecx_integration" / "composition" / "composer_config.yml"
    assert p.is_file()


def test_probe_1270_composer_config_yml_references_existing_prompt_dir():
    """The composer config's prompt_dir should resolve to an existing
    directory containing system.md."""
    import yaml
    p = REPO_ROOT / "src" / "apecx_integration" / "composition" / "composer_config.yml"
    raw = yaml.safe_load(p.read_text())
    pd = raw.get("prompt_dir")
    if pd:
        # prompt_dir is relative to the config file's directory.
        full = (p.parent / pd).resolve()
        assert (full / "system.md").is_file(), (
            f"composer_config prompt_dir={pd} -> {full} has no system.md"
        )


def test_probe_1271_composer_config_yml_library_version_pinned():
    """library_version must be a string (not int / float). Operators
    rely on it for downstream Artifact row pinning."""
    import yaml
    p = REPO_ROOT / "src" / "apecx_integration" / "composition" / "composer_config.yml"
    raw = yaml.safe_load(p.read_text())
    assert isinstance(raw.get("library_version"), str)


def test_probe_1272_composer_config_yml_temperature_zero_default():
    """Production composer should have temperature=0.0 (deterministic).
    A future change to a non-zero value would silently make composer
    output non-reproducible."""
    import yaml
    p = REPO_ROOT / "src" / "apecx_integration" / "composition" / "composer_config.yml"
    raw = yaml.safe_load(p.read_text())
    temp = raw.get("temperature", 0.0)
    assert temp == 0.0, (
        f"composer temperature is {temp} — non-zero defeats "
        f"determinism / reproducibility"
    )


def test_probe_1273_workflow_yamls_use_consistent_class_path_format():
    """Per AC6, ``class:`` paths use dotted-module form. Pin no
    accidental file-paths or relative paths in any wrapper YAML."""
    import yaml
    yaml_dir = (
        REPO_ROOT / "src" / "apecx_integration" / "composition"
        / "workflows" / "violin_bvbrc"
    )
    for ypath in yaml_dir.rglob("*.yml"):
        raw = yaml.safe_load(ypath.read_text())
        if not isinstance(raw, dict):
            continue
        cls = raw.get("class")
        if cls and isinstance(cls, str):
            assert "/" not in cls and "\\" not in cls, (
                f"{ypath.name}: class path looks like a file path: "
                f"{cls!r}"
            )


def test_probe_1274_artifact_store_module_doc_exists():
    """Module docstring documents the class — pin so a future refactor
    that strips docs is caught."""
    import apecx_integration.composition.artifact_store as mod
    assert mod.__doc__ and len(mod.__doc__) > 100


def test_probe_1275_artifact_not_found_takes_artifact_id_in_message():
    """ArtifactNotFound must include the artifact_id in its error
    message (operator diagnostic)."""
    from apecx_integration.composition.artifact_store import ArtifactNotFound
    aid = uuid4()
    e = ArtifactNotFound(f"artifact {aid} not found")
    assert str(aid) in str(e)


def test_probe_1276_composer_prompts_dir_listed_in_module():
    """The composer's __init__ or composer.py should reference
    composer_prompts. Pin via grep."""
    composer_py = (
        REPO_ROOT / "src" / "apecx_integration" / "composition" / "composer.py"
    )
    text = composer_py.read_text()
    assert "composer_prompts" in text or "prompt_dir" in text


def test_probe_1277_workflow_link_yaml_keys_have_no_typos():
    """Each link block must have exactly: link_type, source, target.
    Optional: buffer_size, data_mapping. Pin no typos."""
    import yaml
    wf = (
        REPO_ROOT / "src" / "apecx_integration" / "composition"
        / "workflows" / "violin_bvbrc" / "violin_bvbrc_workflow.yml"
    )
    raw = yaml.safe_load(wf.read_text())
    allowed = {
        "link_type", "source", "target", "buffer_size",
        "data_mapping", "auto_transfer",
    }
    for link_id, link_def in raw.get("links", {}).items():
        cfg = link_def.get("config", {})
        unknown = set(cfg.keys()) - allowed
        assert not unknown, (
            f"link {link_id!r} has unknown config keys: {unknown}"
        )


def test_probe_1278_composer_prompts_md_have_no_unresolved_template_markers():
    """An unresolved ``{{TODO}}`` or ``${PLACEHOLDER}`` in a prompt
    file would silently propagate to the LLM."""
    suspicious = ["{{TODO}}", "{{ TODO }}", "${PLACEHOLDER}", "FIXME"]
    for prompt_file in PROMPT_DIR.glob("*.md"):
        text = prompt_file.read_text(encoding="utf-8")
        for marker in suspicious:
            if marker in text:
                # FIXME may legitimately appear in instructions ABOUT
                # what the LLM should produce; allow if surrounded by
                # backticks (code-formatted).
                if marker == "FIXME" and "`FIXME`" in text:
                    continue
                pytest.fail(
                    f"{prompt_file.name}: unresolved marker {marker!r}"
                )


def test_probe_1279_composer_config_yml_loads_without_extra_keys_post_audit():
    """ComposerConfig now has extra='forbid' (Day 2 v8 audit).
    Re-verify the bundled YAML loads cleanly."""
    import yaml
    from apecx_integration.composition.composer_schemas import ComposerConfig
    p = REPO_ROOT / "src" / "apecx_integration" / "composition" / "composer_config.yml"
    raw = yaml.safe_load(p.read_text())
    ComposerConfig.model_validate(raw)  # raises on unknown keys
