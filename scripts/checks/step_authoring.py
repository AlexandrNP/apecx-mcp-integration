#!/usr/bin/env python
"""TX5 AC3 — nanobrain step-authoring compliance check.

Per ``nanobrain/CLAUDE.md`` § "Method Responsibility Matrix":

    | execute() | infrastructure | ❌ NOT override
    | process() | business logic | ✅ ALWAYS implement

This script enforces both halves of that rule on every file whose
name matches ``*step*.py`` under a target directory:

1. Any class that inherits from ``BaseStep`` (or a name that ends
   in ``Step``, as a proxy) MUST define ``async def process``.
2. No class may define ``def execute`` / ``async def execute`` — the
   framework owns ``execute``; overriding it breaks the infrastructure
   contract.

Fail-loud usage::

    python scripts/checks/step_authoring.py src/
    echo $?   # 0 = compliant, 1 = violation

Rationale: the nanobrain framework validates (1) at step initialization
and raises ``ComponentConfigurationError`` with ``FAIL-FAST: ...``, but
that fires only at load time. Catching the shape at commit time beats
catching it when the first config loads in CI (or worse, production).
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path


def _class_base_names(cls: ast.ClassDef) -> list[str]:
    """Return the raw base-class names for a ClassDef. We only look at
    the surface — if a class inherits from ``foo.BaseStep`` the base
    shows up as ``Attribute`` and we record ``"BaseStep"``.
    """
    names: list[str] = []
    for base in cls.bases:
        if isinstance(base, ast.Name):
            names.append(base.id)
        elif isinstance(base, ast.Attribute):
            names.append(base.attr)
    return names


def _is_step_class(cls: ast.ClassDef) -> bool:
    base_names = _class_base_names(cls)
    # Direct subclass of the framework's BaseStep OR any class whose
    # name ends in ``Step`` (proxy for "subclasses a Step-lineage").
    if "BaseStep" in base_names:
        return True
    # Transitive: we can't resolve subclass chain from a single file,
    # but "name ends in Step" catches intermediate subclasses like
    # ``ConversationalStep(BaseStep) -> ChatStep(ConversationalStep)``.
    if cls.name.endswith("Step") and cls.name != "BaseStep":
        return any(b.endswith("Step") for b in base_names) or not base_names
    return False


def _has_method(cls: ast.ClassDef, name: str, *, async_ok: bool = True) -> bool:
    for item in cls.body:
        if isinstance(item, ast.AsyncFunctionDef) and item.name == name and async_ok:
            return True
        if isinstance(item, ast.FunctionDef) and item.name == name:
            return True
    return False


def check(target: Path) -> list[tuple[Path, str, str]]:
    """Return ``(file, class_name, violation_message)`` for every
    compliance violation found under ``target``."""
    py_files = [p for p in target.rglob("*step*.py") if "__pycache__" not in p.parts]
    violations: list[tuple[Path, str, str]] = []

    for py in py_files:
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if not _is_step_class(node):
                continue
            # Base-class file (nanobrain's BaseStep itself) — skip.
            if node.name == "BaseStep":
                continue

            if not _has_method(node, "process"):
                violations.append((
                    py, node.name,
                    "Step subclass must implement ``async def process(...)`` "
                    "(nanobrain-step-authoring skill — process() carries the business logic).",
                ))

            if _has_method(node, "execute"):
                violations.append((
                    py, node.name,
                    "Step subclass must NOT override ``execute`` — that's "
                    "framework infrastructure. Put the logic in ``process`` instead.",
                ))

    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("target", nargs="?", default="src", help="Directory to scan (default: src).")
    args = parser.parse_args(argv)

    target = Path(args.target).resolve()
    if not target.is_dir():
        print(f"target {target} is not a directory", file=sys.stderr)
        return 2

    violations = check(target)
    if violations:
        print(f"Step-authoring violations in {target}:", file=sys.stderr)
        for py, cls_name, msg in violations:
            rel = py.relative_to(Path.cwd()) if py.is_relative_to(Path.cwd()) else py
            print(f"  {rel}::{cls_name} — {msg}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
