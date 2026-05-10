"""T13 Phase 1 — import-whitelist scanner for LLM-generated Python.

This is the **Phase 1 sandbox**, and it is explicitly a punt per
AP §5.13: we check imports at generation time and refuse to load
novel Python whose imports aren't on the whitelist. **There is no
runtime isolation.** Once the scanner accepts the code, it runs in
the main Tier-2 Python process with full privileges — same venv,
same file-system access, same network.

The Phase-2 Docker-container sandbox (T13b) is where runtime isolation
actually lands. Until then, the scanner + the human review gate
(Step 4 HITL) + operator review are the only safety controls.

## Layering with nanobrain G20 (G36 closure, 2026-05-09)

This scanner is **Stage 1** of a two-layer whitelist. Stage 2 is
nanobrain's ``core/import_whitelist.py`` (G20) which gates ``class:``
paths at YAML load time. The two are intentionally complementary —
see ``docs/whitelist_layering.md`` for the full contract. Tl;dr:
Stage 1 catches LLM-emitted dynamic-import escapes in PYTHON SOURCE;
Stage 2 catches malicious YAML class-path attacks that bypass the
LLM-emit pipeline. Both stay; folding them into one would leave
attack surfaces open at one of the two entry points.

## What the scanner catches

Static imports via Python's ``import`` statement, in every form:

- ``import x``
- ``import x.y.z``
- ``import x as y``
- ``from x import y``
- ``from x.y import z``
- ``from x import y as z``
- ``from . import x`` — relative imports always rejected (novel
  artifacts have no package context, so a relative import is either
  a bug or a dynamic escape attempt).

## What the scanner rejects REGARDLESS of whitelist

Dynamic-import constructs would bypass the whitelist if we only looked
at static imports. So these are banned outright:

- ``importlib.import_module(...)``
- ``__import__(...)``
- ``exec(...)`` / ``eval(...)``
- ``compile(...)`` (can be composed with exec)

These bans apply even to whitelisted modules — you can import
``importlib`` for type-checking purposes, but calling
``importlib.import_module()`` is still blocked. Surface detection only;
we do not try to catch every creative bypass (e.g., reconstructing
``eval`` via ``getattr(__builtins__, 'eva' + 'l')``). A determined
adversary with code-running capability can always escape; the scanner
raises the bar to "obvious".

## What the scanner does NOT catch

- **Dynamic attribute access**: ``getattr(module, 'forbidden_func')``
  on a whitelisted module can still call anything on that module.
  Mitigation: keep the whitelist narrow.
- **Side-effects of whitelisted modules' imports**: if ``pandas`` imports
  ``subprocess`` internally, we allow that — we trust that a library
  on the whitelist has been vetted.
- **Runtime behavior**: as stated above, there is no sandbox at runtime.
  If whitelisted code decides to shell out via subprocess, it will.

## API

    from apecx_integration.composition.sandbox import (
        ImportScanner, ScanViolation, scan_python_source,
    )

    # Raises ScanViolation if disallowed imports / constructs present:
    scan_python_source(source_str, whitelist=my_whitelist)

    # Or use the class for batched / incremental scans:
    scanner = ImportScanner(whitelist=my_whitelist)
    result = scanner.scan(source_str)  # -> ScanResult dataclass
    if result.violations:
        raise ScanViolation(result)
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Banned dynamic-import constructs (always rejected)
# ---------------------------------------------------------------------------

BANNED_CALLS: frozenset[str] = frozenset(
    {
        "__import__",
        "exec",
        "eval",
        "compile",
    }
)

BANNED_ATTRIBUTE_CALLS: frozenset[tuple[str, str]] = frozenset(
    {
        ("importlib", "import_module"),
        ("importlib", "__import__"),
    }
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Import:
    """One import as detected by the scanner."""

    module: str  # top-level package name, e.g. 'pandas' for 'pandas.DataFrame'
    full_path: str  # the full dotted name, e.g. 'pandas.DataFrame'
    lineno: int  # 1-based line number for diagnostics


@dataclass
class ScanResult:
    imports: list[Import] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


class ScanViolation(ValueError):
    """Raised when a scan detects a disallowed construct or import.

    The ``result`` attribute carries the full ``ScanResult`` — callers
    that want to surface all violations at once (instead of just the
    first) can iterate ``exc.result.violations``.

    The optional ``suggestions`` attribute carries "closest matches
    in component library" entries the composer looked up when
    rejecting the novel Python. Surfacing these in the exception
    message steers the LLM (on retry) or the human reviewer back
    toward composition. Empty when the scanner is called standalone
    (e.g. tests that don't need the composer's catalog).
    """

    def __init__(
        self,
        result: ScanResult,
        *,
        suggestions: tuple[str, ...] = (),
    ):
        self.result = result
        self.suggestions = suggestions
        body = "import-scan rejected:\n  " + "\n  ".join(result.violations)
        if suggestions:
            body += "\nClosest matches in component library:\n  " + "\n  ".join(suggestions)
        super().__init__(body)


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


class _ImportVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.imports: list[Import] = []
        self.violations: list[str] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            full = alias.name
            top = full.split(".", 1)[0]
            self.imports.append(Import(module=top, full_path=full, lineno=node.lineno))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level != 0:
            # Relative import — novel artifacts are single-file, no package context.
            self.violations.append(
                f"line {node.lineno}: relative imports are not allowed in novel artifacts "
                f"({'.' * node.level}{node.module or ''})"
            )
            return
        full = node.module or ""
        top = full.split(".", 1)[0] if full else ""
        for alias in node.names:
            child_full = f"{full}.{alias.name}" if full else alias.name
            self.imports.append(Import(module=top, full_path=child_full, lineno=node.lineno))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id in BANNED_CALLS:
            self.violations.append(
                f"line {node.lineno}: banned dynamic-code construct: {func.id}()"
            )
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            pair = (func.value.id, func.attr)
            if pair in BANNED_ATTRIBUTE_CALLS:
                self.violations.append(
                    f"line {node.lineno}: banned dynamic-import construct: "
                    f"{func.value.id}.{func.attr}()"
                )
        self.generic_visit(node)


class ImportScanner:
    """Scan Python source for imports and banned dynamic-code constructs."""

    def __init__(self, *, whitelist: frozenset[str] | None = None) -> None:
        self._whitelist = whitelist

    def scan(self, source: str) -> ScanResult:
        result = ScanResult()
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            result.violations.append(f"syntax error at line {exc.lineno}: {exc.msg}")
            return result

        visitor = _ImportVisitor()
        visitor.visit(tree)
        result.imports = visitor.imports
        result.violations = list(visitor.violations)

        if self._whitelist is not None:
            for imp in visitor.imports:
                if imp.module and imp.module not in self._whitelist:
                    result.violations.append(
                        f"line {imp.lineno}: import '{imp.full_path}' not on the "
                        f"sandbox whitelist (top-level package: '{imp.module}')"
                    )

        return result


# ---------------------------------------------------------------------------
# Whitelist loader
# ---------------------------------------------------------------------------


def load_whitelist(path: str | Path) -> frozenset[str]:
    """Load a newline-delimited whitelist file.

    Lines starting with ``#`` and blank lines are ignored. Each remaining
    line is one top-level package name (e.g. ``pandas``, not
    ``pandas.DataFrame``). Duplicates are silently collapsed.
    """
    p = Path(path)
    raw = p.read_text(encoding="utf-8").splitlines()
    entries: set[str] = set()
    for line in raw:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "." in stripped:
            raise ValueError(
                f"whitelist entry '{stripped}' contains a '.' — entries must be "
                "top-level package names only (e.g. 'pandas', not 'pandas.DataFrame')"
            )
        entries.add(stripped)
    return frozenset(entries)


# ---------------------------------------------------------------------------
# Convenience entry point
# ---------------------------------------------------------------------------


def scan_python_source(source: str, *, whitelist: frozenset[str] | None = None) -> ScanResult:
    """Top-level scan: parse, detect imports + dynamic-code constructs,
    apply whitelist if provided. Returns a ``ScanResult`` whose
    ``ok`` property tells you pass/fail. Raises ``ScanViolation`` via
    ``raise_if_violated`` if you want a thrown error instead::

        result = scan_python_source(source, whitelist=load_whitelist(...))
        if not result.ok:
            raise ScanViolation(result)
    """
    return ImportScanner(whitelist=whitelist).scan(source)
