"""Deterministic AST structural validation of LLM-emitted nanobrain Python.

Single source of truth for "is this generated Python structurally sound + does
it obey the framework's hard rules" — shared by:

* ``CodeStructureValidatorStep`` (the review-revise gate for codegen), and
* the composer's novel-Python acceptance check (so a hallucinated/broken novel
  step is rejected at COMPOSE time with an actionable critique, instead of
  passing the import-scan and only failing at workflow-run time).

Deterministic, no exec, no LLM, no subprocess — pure ``ast``. Checks (100%
precision, by design — only complain on a real AST-detectable problem):

1. Unparseable code (SyntaxError).
2. Missing entry point (when supplied) at module scope.
3. ``from_config`` override on a framework-base subclass (-> RuntimeError at load).
4. ``execute`` override on a framework-base subclass (forbidden by the framework).
5. (optional) BaseStep subclass without ``async def process``.
6. Imports from non-existent ``nanobrain.*`` submodules (hallucinations).
"""

from __future__ import annotations

import ast

# Valid nanobrain package ROOTS. An import is accepted iff its module equals a
# root or is under it (``root.<submodule>``). Roots, not a leaf-module list, so
# every REAL ``nanobrain.core.*`` submodule (e.g. ``nanobrain.core.component_base``)
# passes — a leaf whitelist silently false-flagged legit core imports at compose
# time. Truly hallucinated top-level subpackages (``nanobrain.utils``,
# ``nanobrain.helpers``) match no root and are still flagged.
_NANOBRAIN_WHITELIST: frozenset[str] = frozenset(
    {
        "nanobrain.core",
        "nanobrain.lightweight",
        "nanobrain.library",
        "nanobrain.academy_integration",
    }
)

# Base classes whose subclasses MUST NOT override from_config or execute.
_FRAMEWORK_BASES: frozenset[str] = frozenset({"BaseStep", "ToolBase", "Workflow", "BaseAgent"})


def _collect_top_level_names(tree: ast.Module) -> set[str]:
    """Names defined at module scope (classes + functions + assignments)."""
    out: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out.add(target.id)
    return out


def _inherits_base_class(cls: ast.ClassDef, name: str) -> bool:
    for base in cls.bases:
        # Handle both ``BaseStep`` and ``nanobrain.core.step.BaseStep``.
        if isinstance(base, ast.Name) and base.id == name:
            return True
        if isinstance(base, ast.Attribute) and base.attr == name:
            return True
    return False


def _inherits_framework_base(cls: ast.ClassDef) -> bool:
    """True if the class declares any framework base in its bases (direct only)."""
    return any(_inherits_base_class(cls, name) for name in _FRAMEWORK_BASES)


def validate_python_structure(
    code: str,
    entry_point: str = "",
    *,
    strict_imports: bool = True,
    require_process: bool = False,
) -> list[str]:
    """Run AST structural checks. Returns an ordered list of issue strings;
    an empty list means the code passes every check. Never executes the code.
    """
    issues: list[str] = []

    # Check 1: syntax.
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"SyntaxError at line {e.lineno}: {e.msg}"]

    top_level_names = _collect_top_level_names(tree)
    class_defs = [n for n in tree.body if isinstance(n, ast.ClassDef)]

    # Check 2: entry_point present at module scope (if requested).
    if entry_point and entry_point not in top_level_names:
        issues.append(
            f"Required entry point ``{entry_point}`` is not defined at module "
            f"scope. Found names: {sorted(top_level_names)[:6]}..."
        )

    # Check 3 + 4: from_config / execute overrides on framework-class subclasses.
    for cls in class_defs:
        if not _inherits_framework_base(cls):
            continue
        for item in cls.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if item.name == "from_config":
                issues.append(
                    f"Class ``{cls.name}`` overrides ``from_config`` — remove "
                    f"it; the framework's inherited ``from_config`` is the "
                    f"only correct path. Direct instantiation via "
                    f"``cls(...)`` raises ``RuntimeError``."
                )
            if item.name == "execute":
                issues.append(
                    f"Class ``{cls.name}`` overrides ``execute`` — remove "
                    f"it; the framework forbids overriding execute(). "
                    f"Implement ``async def process`` instead."
                )

        # Check 5: BaseStep subclass without process() (optional).
        if require_process and _inherits_base_class(cls, "BaseStep"):
            has_process = any(
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "process"
                for item in cls.body
            )
            if not has_process:
                issues.append(
                    f"Class ``{cls.name}`` inherits BaseStep but does not "
                    f"define ``async def process``. The framework requires "
                    f"process() implementations on every BaseStep subclass."
                )

    # Check 6: hallucinated nanobrain imports.
    if strict_imports:
        for stmt in ast.walk(tree):
            if (
                isinstance(stmt, ast.ImportFrom)
                and stmt.module
                and stmt.module.startswith("nanobrain.")
                and not any(
                    stmt.module == w or stmt.module.startswith(w + ".")
                    for w in _NANOBRAIN_WHITELIST
                )
            ):
                issues.append(
                    f"Import ``from {stmt.module} import ...`` references "
                    f"a non-existent nanobrain submodule. Valid roots: "
                    f"{sorted(_NANOBRAIN_WHITELIST)[:5]}..."
                )

    return issues


__all__ = ["validate_python_structure"]
