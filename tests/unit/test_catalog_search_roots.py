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
