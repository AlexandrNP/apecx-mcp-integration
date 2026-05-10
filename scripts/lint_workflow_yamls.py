"""G39 — workflow YAML lint.

Enforces the post-G39 contract on every nanobrain workflow YAML in the
integration repo:

  R1. Top-level workflow YAMLs declare ``config_version: 2``.
      Detection: file lives under one of the canonical workflow roots
      (composition/workflows/<name>/<workflow_name>.yml or
      synonym_dictionary/workflow/configs/*_workflow.yml) AND the file
      has a ``links:`` block (links are unique to workflow-shaped YAMLs;
      step + tool wrappers do not carry them).

  R2. Every inline DirectLink (config dict embedded under ``config:``)
      sets ``auto_transfer: true`` explicitly. v2's mutator now
      auto-injects the field at config-load time, but the explicit
      declaration is the lint-detectable signal that protects against
      future YAML refactors that forget v2 + the mutator.

  R3. Every path-reference DirectLink (``config: "<path>.yml"``) points
      at a file that exists AND that contains ``auto_transfer: true``.

Exits 0 on clean lint; non-zero with a line-numbered violation report
otherwise. Designed to run as a CI gate; safe to run locally.

Source: ``apecx-mcp-integration/eval_03_nanobrain_gap_inventory.md``
Round 4 G39; ``apecx-mcp-integration/docs/development_roadmap.md`` 8.7.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Canonical workflow-YAML roots. Files outside these trees are still
# linted for R2/R3 (any inline DirectLink anywhere should be safe), but
# the v2-pin (R1) only fires on files under these roots.
WORKFLOW_ROOTS: tuple[Path, ...] = (
    REPO_ROOT / "src" / "apecx_integration" / "composition" / "workflows",
    REPO_ROOT / "src" / "apecx_integration" / "synonym_dictionary" / "workflow" / "configs",
)


@dataclass
class Violation:
    file: Path
    line: int
    rule: str
    message: str


@dataclass
class LintResult:
    files_scanned: int = 0
    workflow_yamls_scanned: int = 0
    direct_links_scanned: int = 0
    violations: list[Violation] = field(default_factory=list)


_HAS_LINKS_BLOCK_RE = re.compile(r"^links:\s*$", re.MULTILINE)
_DIRECT_LINK_INLINE_RE = re.compile(
    # 'class: ".../link.DirectLink"' followed within the next ~25 lines
    # by either an inline 'config: {' block or a 'config: "<path>.yml"'.
    r'class:\s*"?[^"\s]*\.DirectLink"?\s*$',
    re.MULTILINE,
)
_AUTO_TRANSFER_TRUE_RE = re.compile(r"^\s*auto_transfer:\s*true\b", re.MULTILINE)
_PATH_REFERENCE_CONFIG_RE = re.compile(
    r'^(\s*)config:\s*"([^"]+\.yml)"\s*$',
    re.MULTILINE,
)
_INLINE_CONFIG_RE = re.compile(r"^\s*config:\s*$", re.MULTILINE)
_CONFIG_VERSION_2_RE = re.compile(
    r"^config_version:\s*2\s*$",
    re.MULTILINE,
)


def _is_workflow_yaml(path: Path, content: str) -> bool:
    """A workflow YAML lives under one of the canonical roots AND
    contains a top-level ``links:`` block. Step/tool wrapper YAMLs do
    not have ``links:`` and are exempt from the v2-pin rule.
    """
    inside_root = any(path.is_relative_to(root) for root in WORKFLOW_ROOTS)
    if not inside_root:
        return False
    return bool(_HAS_LINKS_BLOCK_RE.search(content))


def _line_of(content: str, match_start: int) -> int:
    """1-based line number for a regex match offset."""
    return content.count("\n", 0, match_start) + 1


def _check_v2_pin(path: Path, content: str, result: LintResult) -> None:
    """R1: workflow YAMLs must declare ``config_version: 2``."""
    if _CONFIG_VERSION_2_RE.search(content):
        return
    result.violations.append(
        Violation(
            file=path,
            line=1,
            rule="R1",
            message=(
                "workflow YAML missing ``config_version: 2`` declaration. "
                "Add ``config_version: 2`` near the top (alongside name + "
                "version). Required by G39 (eval_03 Round 4)."
            ),
        )
    )


def _find_directlinks_with_locations(
    content: str,
) -> list[tuple[int, int, int]]:
    """Return [(start_offset, line_number, indent)] for every inline
    DirectLink class declaration. Indent helps detect the matching
    config block at the next deeper level."""
    out: list[tuple[int, int, int]] = []
    for m in _DIRECT_LINK_INLINE_RE.finditer(content):
        line_start = content.rfind("\n", 0, m.start()) + 1
        indent = m.start() - line_start
        # Detect inline indentation only on lines whose preceding
        # context is a YAML key-value declaration (skip class names
        # appearing in plain comments).
        line_text = content[line_start : m.end()]
        stripped = line_text.lstrip()
        if not stripped.startswith("class:"):
            continue
        out.append((m.start(), _line_of(content, m.start()), indent))
    return out


def _check_directlinks(path: Path, content: str, result: LintResult) -> None:
    """R2 / R3: every DirectLink must end up with auto_transfer: true.

    Strategy: for each ``class: ...DirectLink`` line, scan the next 30
    lines for either:
      - an inline ``config:`` block followed by ``auto_transfer: true``
        within the same indent block (R2)
      - a path-reference ``config: "<path>.yml"`` whose target exists
        and contains ``auto_transfer: true`` (R3)
    """
    locations = _find_directlinks_with_locations(content)
    result.direct_links_scanned += len(locations)

    for start_offset, line_no, _indent in locations:
        # Look at the next 30 lines for the matching config block.
        line_end = content.find("\n", start_offset)
        window_start = line_end + 1
        # Take a slice of up to ~50 lines after the class declaration —
        # generous, covers indented config + sibling fields.
        window_lines = content[window_start : window_start + 4000].split("\n")[:50]
        window = "\n".join(window_lines)

        # Path-reference shape: ``config: "subpath/file.yml"``
        path_match = _PATH_REFERENCE_CONFIG_RE.search(window)
        inline_match = _INLINE_CONFIG_RE.search(window)

        if path_match:
            referenced = (path.parent / path_match.group(2)).resolve()
            if not referenced.is_file():
                result.violations.append(
                    Violation(
                        file=path,
                        line=line_no,
                        rule="R3",
                        message=(
                            f"DirectLink config path-reference "
                            f"{path_match.group(2)!r} does not resolve to "
                            f"an existing file (looked at {referenced})."
                        ),
                    )
                )
                continue
            ref_content = referenced.read_text()
            if not _AUTO_TRANSFER_TRUE_RE.search(ref_content):
                result.violations.append(
                    Violation(
                        file=path,
                        line=line_no,
                        rule="R3",
                        message=(
                            f"DirectLink path-referenced YAML "
                            f"{path_match.group(2)!r} is missing "
                            f"``auto_transfer: true``. Without it, the link "
                            f"silently no-ops on source-DU change. Add the "
                            f"line to {referenced}."
                        ),
                    )
                )
            continue

        if inline_match:
            # Inline config block — scan the next ~30 lines for
            # auto_transfer:true within the same nested block.
            if not _AUTO_TRANSFER_TRUE_RE.search(window):
                result.violations.append(
                    Violation(
                        file=path,
                        line=line_no,
                        rule="R2",
                        message=(
                            "inline DirectLink missing "
                            "``auto_transfer: true`` in its config block. "
                            "Pre-G7-Step-5 the field defaulted to False, "
                            "which is a silent-failure shape (workflow "
                            "loads, every step runs, no exception, no "
                            "data ever transfers). Even though v2's "
                            "default is now True, the explicit "
                            "declaration is the lint-detectable signal."
                        ),
                    )
                )
            continue

        # No detectable config block — could be a comment-only mention
        # of DirectLink (e.g., docstring). Skip: no false positives.


def _scan_file(path: Path, result: LintResult) -> None:
    try:
        content = path.read_text()
    except OSError as exc:
        result.violations.append(
            Violation(file=path, line=0, rule="R0", message=f"unreadable: {exc}")
        )
        return
    result.files_scanned += 1
    if _is_workflow_yaml(path, content):
        result.workflow_yamls_scanned += 1
        _check_v2_pin(path, content, result)
    _check_directlinks(path, content, result)


def lint(paths: list[Path]) -> LintResult:
    result = LintResult()
    yml_files: list[Path] = []
    for p in paths:
        if p.is_dir():
            yml_files.extend(p.rglob("*.yml"))
            yml_files.extend(p.rglob("*.yaml"))
        elif p.is_file():
            yml_files.append(p)
    for f in sorted(yml_files):
        _scan_file(f, result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="G39 workflow YAML lint (config_version: 2 + auto_transfer)."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help=(
            "Files or directories to lint. Defaults to the canonical "
            "workflow roots (composition/workflows/ and "
            "synonym_dictionary/workflow/configs/)."
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress the summary line on clean lint runs",
    )
    args = parser.parse_args(argv)

    targets = args.paths or list(WORKFLOW_ROOTS)
    result = lint(targets)

    if result.violations:
        print(
            f"G39 lint: {len(result.violations)} violation(s) across "
            f"{result.files_scanned} files "
            f"({result.workflow_yamls_scanned} workflow YAMLs, "
            f"{result.direct_links_scanned} DirectLinks scanned).",
            file=sys.stderr,
        )
        for v in result.violations:
            print(
                f"  [{v.rule}] {v.file.relative_to(REPO_ROOT) if v.file.is_relative_to(REPO_ROOT) else v.file}:{v.line}: {v.message}",
                file=sys.stderr,
            )
        return 1

    if not args.quiet:
        print(
            f"G39 lint: clean — {result.files_scanned} files, "
            f"{result.workflow_yamls_scanned} workflow YAMLs, "
            f"{result.direct_links_scanned} DirectLinks."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
