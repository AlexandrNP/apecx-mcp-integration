"""Regression test — every shippable resource under ``src/apecx_integration/``
is covered by ``[tool.setuptools.package-data]`` in ``pyproject.toml``.

Why this test exists
--------------------
The per-directory globs that used to live in ``package-data`` silently
missed files when new workflow subdirectories were added. The wheel
built without errors; the install copy was missing YAMLs; the crash
only surfaced at runtime when the missing file was actually loaded.

Concrete failure mode (2026-05-06): ``rag_e2e_synthesis/manifest.yml``
was added but the corresponding ``composition/workflows/rag_e2e_synthesis/*.yml``
glob was never appended to ``package-data``. ``uv tool install`` produced
a wheel without the file. ``apecx-cp serve`` crashed at startup with
``FileNotFoundError`` when the composer tried to load the missing manifest.

What this test asserts
----------------------
1. Every file with an extension in :data:`EXPECTED_RESOURCE_EXTS` under
   ``src/apecx_integration/`` matches at least one glob in the
   ``[tool.setuptools.package-data]`` declaration.
2. The declaration's globs are valid (don't reference paths outside
   ``apecx_integration/``).

If you add a new file with an extension not in
:data:`EXPECTED_RESOURCE_EXTS` that needs to ship, expand BOTH the set
and the package-data globs together. The test fails loudly when the
two are out of sync.
"""

from __future__ import annotations

import tomllib
from fnmatch import fnmatch
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
PKG_ROOT = REPO_ROOT / "src" / "apecx_integration"

# File extensions whose contents must be readable from inside the
# installed wheel (i.e. must be declared in package-data). If you add
# a new shippable resource type, add its extension here and to
# pyproject.toml's package-data globs.
EXPECTED_RESOURCE_EXTS: frozenset[str] = frozenset(
    {".yml", ".yaml", ".md", ".ini", ".txt", ".fasta"}
)


def _load_package_data_globs() -> list[str]:
    raw = tomllib.loads(PYPROJECT.read_text())
    return list(raw["tool"]["setuptools"]["package-data"]["apecx_integration"])


def _all_resource_files() -> list[Path]:
    """Every shippable resource under apecx_integration/, relative to the package root."""
    out: list[Path] = []
    for path in PKG_ROOT.rglob("*"):
        if not path.is_file():
            continue
        # Skip __pycache__ and other tooling artifacts setuptools also skips.
        if "__pycache__" in path.parts:
            continue
        if path.suffix.lower() in EXPECTED_RESOURCE_EXTS:
            out.append(path.relative_to(PKG_ROOT))
    return out


def _glob_matches(rel_path: Path, glob: str) -> bool:
    """Match a single file path against a setuptools-style package-data glob.

    Setuptools accepts ``**`` for recursive descent and ``*`` for single-level
    wildcards. ``fnmatch`` doesn't natively understand ``**``; emulate it by
    treating ``**`` as "any number of path segments" via path-aware matching.
    """
    parts = rel_path.parts
    glob_parts = Path(glob).parts

    def _match(g: tuple[str, ...], p: tuple[str, ...]) -> bool:
        # Greedy recursive match for ``**``.
        if not g:
            return not p
        if g[0] == "**":
            # Match zero or more segments.
            if _match(g[1:], p):
                return True
            if not p:
                return False
            return _match(g, p[1:])
        if not p:
            return False
        if not fnmatch(p[0], g[0]):
            return False
        return _match(g[1:], p[1:])

    return _match(glob_parts, parts)


def test_every_resource_under_package_root_is_in_package_data() -> None:
    globs = _load_package_data_globs()
    files = _all_resource_files()

    uncovered = [f for f in files if not any(_glob_matches(f, g) for g in globs)]

    if uncovered:
        sample = "\n".join(f"  - {f}" for f in uncovered[:20])
        more = f"  (+{len(uncovered) - 20} more)" if len(uncovered) > 20 else ""
        raise AssertionError(
            f"{len(uncovered)} resource file(s) under src/apecx_integration/ "
            f"are NOT covered by [tool.setuptools.package-data] in pyproject.toml. "
            f"Wheels built from this repo will be missing them — the runtime crash "
            f"will be a FileNotFoundError or yaml-load error.\n\n"
            f"Add a covering glob to package-data, or remove the file if it "
            f"shouldn't ship. Uncovered files (first 20):\n{sample}\n{more}"
        )


def test_package_data_declaration_is_well_formed() -> None:
    """Every glob in package-data must be a relative path with no parent refs."""
    globs = _load_package_data_globs()
    bad = [g for g in globs if g.startswith("/") or ".." in Path(g).parts]
    assert not bad, f"package-data globs must be relative; got: {bad}"


def test_resource_extensions_are_in_sync() -> None:
    """Every file extension shipped by package-data globs is declared in
    EXPECTED_RESOURCE_EXTS. Catches the case where someone adds a glob like
    ``**/*.json`` to package-data but forgets to add ``.json`` to the test's
    expected-set, which would let future ``.json`` files slip past
    test_every_resource_under_package_root_is_in_package_data."""
    globs = _load_package_data_globs()
    glob_exts: set[str] = set()
    for g in globs:
        suffix = Path(g).suffix.lower()
        if suffix and "*" not in suffix:
            glob_exts.add(suffix)
    drift = glob_exts - EXPECTED_RESOURCE_EXTS
    assert not drift, (
        f"package-data declares globs for extensions {sorted(drift)} but "
        f"EXPECTED_RESOURCE_EXTS does not list them. Add them to the set "
        f"or remove the globs."
    )


def test_pymol_container_build_context_is_in_package_data() -> None:
    """The headless-PyMOL docker build context (``Dockerfile`` + ``_pymol_job.py``) ships as DATA,
    not as a module (the job imports ``pymol2``, unimportable on the host). The generic resource
    scan above only checks EXPECTED_RESOURCE_EXTS (yml/md/…), so a no-extension Dockerfile + a
    ``.py``-as-data would slip past it — assert them EXPLICITLY. This pins the exact hole that let
    the old repo-root ``docker/pymol/`` ship a wheel missing ``_pymol_job.py`` → the SASA leg's
    first-use auto-build (``ensure_image``) crashed with FileNotFoundError on a uv-tool / wheel install."""
    globs = _load_package_data_globs()
    container_dir = PKG_ROOT / "composition" / "steps" / "_pymol_container"
    assert container_dir.is_dir(), f"PyMOL build context missing at {container_dir}"
    for name in ("Dockerfile", "_pymol_job.py"):
        f = container_dir / name
        assert f.is_file(), f"expected build-context file missing: {f}"
        rel = f.relative_to(PKG_ROOT)
        assert any(_glob_matches(rel, g) for g in globs), (
            f"{rel} is NOT covered by any [tool.setuptools.package-data] glob — the wheel would "
            f"omit it and the PyMOL SASA leg's first-use auto-build would FileNotFoundError on a "
            f"non-editable install."
        )
