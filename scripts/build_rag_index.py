"""Build a ComponentIndex FAISS artifact from a composer config.

Reads ``component_catalog_paths`` + ``library_version`` out of a
``ComposerConfig`` YAML, runs ``ComponentIndex.rebuild`` over the
listed manifests, and writes the ``faiss.bin`` + ``metadata.json``
pair to ``<config_dir>/rag_index`` by default (or to the path
supplied via ``--out``).

Why it lives outside the composer
---------------------------------
Embedding the catalog takes ~5s on warm cache, ~60s cold. Doing
that inside ``Composer.compose()`` would block every first
``compose()`` call after process start. Keeping the build step
offline + on-disk means the composer's ``rag_index_dir`` is a fast
``load()`` (~100ms), which is the right latency profile for an
interactive composer.

Usage
-----
::

    PYTHONPATH=... python scripts/build_rag_index.py \\
        src/apecx_integration/composition/composer_config.yml

    # Then in composer_config.yml add:
    #   rag_index_dir: "rag_index"   # relative to this config file
"""

# ruff: noqa: I001, E402
# Load order is load-bearing: sentence_transformers (inside
# ``nanobrain.lightweight.component_index``) MUST import before faiss,
# or SentenceTransformer.encode() silently segfaults on macOS ARM.
# See session_friction_log.md #13 for the full detection signal.
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from nanobrain.lightweight.component_index import ComponentIndex

import yaml


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__ or "")
    p.add_argument(
        "config",
        type=Path,
        help="Path to composer_config.yml",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "Where to write the index (faiss.bin + metadata.json). "
            "Default: <config_dir>/rag_index."
        ),
    )
    return p.parse_args(argv)


def build(config_path: Path, out_dir: Path | None = None) -> Path:
    if not config_path.is_file():
        raise FileNotFoundError(f"composer config not found: {config_path}")
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ValueError(
            f"composer config must be a mapping; got {type(cfg).__name__}"
        )

    raw_paths = cfg.get("component_catalog_paths") or []
    library_version = cfg.get("library_version")
    if not library_version:
        raise ValueError("composer config missing 'library_version'")

    manifest_paths: list[Path] = []
    for entry in raw_paths:
        p = Path(entry)
        if not p.is_absolute():
            p = (config_path.parent / p).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"manifest not found: {p}")
        manifest_paths.append(p)

    if not manifest_paths:
        raise ValueError(
            "composer config lists no component_catalog_paths — nothing "
            "to index."
        )

    target = out_dir or (config_path.parent / "rag_index")
    idx = ComponentIndex()
    idx.rebuild(
        manifest_paths=manifest_paths, library_version=library_version
    )
    idx.save(target)

    # Audit §3.9: post-save verification. Pre-fix, if `idx.save`
    # raised mid-write (disk full, permission flap), the print
    # below didn't fire and the operator was left guessing whether
    # any files reached disk. The verification here surfaces a
    # partial-write as an explicit RuntimeError, naming the missing
    # file so the operator can clean up before retrying.
    expected = ("faiss.bin", "metadata.json")
    missing: list[str] = []
    for name in expected:
        path = target / name
        if not path.is_file() or path.stat().st_size == 0:
            missing.append(name)
    if missing:
        raise RuntimeError(
            f"[build_rag_index] post-save verification failed at "
            f"{target}: expected non-empty {expected!r}, missing or "
            f"empty: {missing!r}. The save partially completed; "
            "inspect / clean up before retrying."
        )

    print(
        f"[build_rag_index] wrote {len(idx)} components to {target} "
        f"(hash={idx.index_hash[:12]}); verified faiss.bin "
        f"({(target / 'faiss.bin').stat().st_size} B) and "
        f"metadata.json ({(target / 'metadata.json').stat().st_size} B)"
    )
    return target


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        build(args.config.resolve(), args.out)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
