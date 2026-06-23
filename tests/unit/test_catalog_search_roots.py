"""T4: catalog_search_roots — the dirs the executor injects as config_search_paths
so a composed workflow reusing wrappers from multiple catalog dirs resolves at run
time. Real composer_config, no mocks. (The full executor load+run e2e is T8.)
"""

from __future__ import annotations

from pathlib import Path

import apecx_integration
from apecx_integration.composition.component_catalog import catalog_search_roots

_CFGDIR = Path(apecx_integration.__file__).parent / "composition"


def test_real_config_yields_the_four_manifest_dirs():
    roots = catalog_search_roots(_CFGDIR / "composer_config.yml")
    names = {Path(r).name for r in roots}
    assert names == {"rag_e2e_synthesis", "code_writing", "_reasoning_patterns", "_catalog_steps"}
    assert all(Path(r).is_absolute() and Path(r).is_dir() for r in roots)


def test_default_arg_uses_canonical_composer_config():
    # No arg -> the canonical composition/composer_config.yml, so an executor that wasn't
    # explicitly wired still resolves catalog components (the forget-to-wire robustness fix).
    default_roots = catalog_search_roots()
    explicit_roots = catalog_search_roots(_CFGDIR / "composer_config.yml")
    assert default_roots == explicit_roots
    # The _catalog_steps root (which holds entity_extraction.yml) is present by default.
    assert any((Path(r) / "entity_extraction.yml").is_file() for r in default_roots)


def test_executor_defaults_roots_when_omitted_but_optout_on_empty(tmp_path):
    # B (robustness): LocalExecutor that wasn't wired (arg omitted -> None) gets the canonical
    # catalog roots so a composed workflow referencing catalog components still resolves; an
    # EXPLICIT [] opts out. No LLM/DB needed — __init__ only stores these deps.
    from apecx_integration.control_plane.executors.local import LocalExecutor

    omitted = LocalExecutor(
        session_factory=None, artifact_store=None, recorder=None, workflow_base_dir=tmp_path
    )
    expected = [str(Path(p).resolve()) for p in catalog_search_roots()]
    assert omitted._config_search_paths == expected
    assert omitted._config_search_paths  # non-empty default roots (the forget-to-wire fix)

    optout = LocalExecutor(
        session_factory=None,
        artifact_store=None,
        recorder=None,
        workflow_base_dir=tmp_path,
        config_search_paths=[],
    )
    assert optout._config_search_paths == []


def test_missing_config_is_empty_noop(tmp_path):
    assert catalog_search_roots(tmp_path / "nope.yml") == []


def test_dedups_and_resolves(tmp_path):
    # Two manifest entries under the same dir -> one root; relative entries resolve
    # against the config dir.
    (tmp_path / "m1").mkdir()
    (tmp_path / "m1" / "manifest.yml").write_text("components: []\n")
    (tmp_path / "m2").mkdir()
    (tmp_path / "m2" / "manifest.yml").write_text("components: []\n")
    cfg = tmp_path / "composer_config.yml"
    cfg.write_text(
        "component_catalog_paths:\n"
        "  - m1/manifest.yml\n"
        "  - m2/manifest.yml\n"
        "  - m1/manifest.yml\n",  # duplicate -> deduped
        encoding="utf-8",
    )
    roots = catalog_search_roots(cfg)
    assert roots == [str((tmp_path / "m1").resolve()), str((tmp_path / "m2").resolve())]
