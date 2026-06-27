#!/usr/bin/env python
"""TX5 AC2 — reject any PR whose ``src/`` imports a package that's not
installable in the current venv.

Parses every ``.py`` under a target directory (default ``src/``) for
``import X`` / ``from X import Y`` statements, collects the set of
unique top-level module names, then uses ``importlib.util.find_spec``
to verify each can be located — without actually importing the module
(so we don't pay import-time side effects for `os` / `sys` / etc.).

Fail-loud usage::

    python scripts/checks/imports_resolve.py src/
    echo $?   # 0 = all imports resolve, 1 = one or more missing

Stdlib modules, relative imports, and modules that are first-party
(their top-level name matches a directory under the target) are skipped —
they don't need to be on PyPI to resolve.

Rationale: faster than ``pytest --collect-only`` because it short-
circuits on find_spec and doesn't execute module code. Catches "agent
hallucinated an import for a package that doesn't exist" (R11 per the
architectural plan) before the PR lands.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import sys
from pathlib import Path


def _collect_top_level_imports(py_file: Path) -> set[str]:
    """Return the set of top-level module names imported by ``py_file``."""
    try:
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
    except SyntaxError:
        # If the file doesn't even parse, ruff will catch that separately —
        # not this check's responsibility.
        return set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module.split(".", 1)[0])
    return modules


def _first_party_names(target: Path) -> set[str]:
    """Top-level Python package names inside the target directory.

    For ``src/`` layout these are the names of top-level subdirs that
    contain an ``__init__.py`` or ``*.py`` file. We skip these during
    resolution because they may not be installed yet (the package under
    test IS the one we're checking).
    """
    names: set[str] = set()
    if not target.is_dir():
        return names
    for child in target.iterdir():
        if not child.is_dir():
            continue
        if (child / "__init__.py").is_file() or any(child.glob("*.py")):
            names.add(child.name)
    return names


def check(target: Path) -> list[tuple[str, Path]]:
    """Return ``(module_name, first_py_file_that_imports_it)`` pairs for
    every import whose spec can't be found.
    """
    py_files = list(target.rglob("*.py"))
    first_party = _first_party_names(target)

    per_module_first_seen: dict[str, Path] = {}
    for py in py_files:
        if "__pycache__" in py.parts:
            continue
        # Container build-artifact scripts (``_pymol_container/_pymol_job.py``) are NEVER imported
        # on the host — they are copied into a docker image and run there, so they import
        # container-only modules (``pymol2``, the sibling ``_pymol_sasa`` copied in alongside).
        # They ship as packaged build-context data, not host code; skip them in this host-import lint.
        if "_pymol_container" in py.parts:
            continue
        for mod in _collect_top_level_imports(py):
            per_module_first_seen.setdefault(mod, py)

    failures: list[tuple[str, Path]] = []
    for mod, first_py in per_module_first_seen.items():
        if mod in first_party:
            continue
        if mod in sys.stdlib_module_names:
            continue
        try:
            spec = importlib.util.find_spec(mod)
        except (ImportError, ValueError):
            spec = None
        if spec is None:
            failures.append((mod, first_py))
    failures.sort()
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "target", nargs="?", default="src", help="Directory to scan (default: src)."
    )
    args = parser.parse_args(argv)

    target = Path(args.target).resolve()
    if not target.is_dir():
        print(f"target {target} is not a directory", file=sys.stderr)
        return 2

    failures = check(target)
    if failures:
        print(f"Unresolvable imports in {target}:", file=sys.stderr)
        for mod, first_py in failures:
            rel = (
                first_py.relative_to(Path.cwd())
                if first_py.is_relative_to(Path.cwd())
                else first_py
            )
            print(f"  {mod!r}  (first seen in {rel})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
