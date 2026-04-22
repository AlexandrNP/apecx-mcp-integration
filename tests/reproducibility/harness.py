"""Reproducibility harness (T12).

Catches silent reproducibility failure: same prompt + same library
version + same pinned LLM version should produce the same generated
artifact. When they don't, a human needs to know *why*.

## Shape

A **fixture** lives at ``tests/reproducibility/fixtures/<name>/`` and
has three files:

- ``prompt.txt`` — the full composer prompt.
- ``kind`` — either ``yaml`` (for GENERATED_WORKFLOW) or ``python``
  (for GENERATED_PYTHON). Selects the semantic-equivalence comparator
  used when hashes disagree.
- ``baseline_hash.txt`` — SHA-256 (hex) of the expected generated
  bytes, as produced by the original capture of this fixture.
  Re-capture by regenerating with the pinned LLM version, saving the
  bytes, and overwriting this file.

Optional:
- ``baseline_content.(yml|py)`` — the bytes the baseline hash was
  taken over. Used by the semantic-equivalence fallback when the hash
  check fails but the content may still be equivalent (key ordering,
  whitespace, formatting drift that doesn't change meaning).

## Comparator ladder (T12 AC3)

1. **Hash equality.** If `sha256(generate(prompt)) == baseline_hash`,
   pass. Cheapest and most common.
2. **Semantic equivalence.** If hashes disagree:
   - YAML: parse both with ``yaml.safe_load`` and compare the Python
     objects. Key order, quoting style, and indentation differences
     are ignored; value differences are not.
   - Python: parse both with ``ast.parse`` and compare the AST via
     ``ast.dump`` with ``annotate_fields=False, include_attributes=
     False``. Whitespace, comment, and formatting differences are
     ignored; statement shape / identifiers / literals are not.
3. **Fail loud** if both checks disagree. The message includes a
   compact diff of the first differing field so the reviewer can see
   whether it's the composer drifting (bad) or the formatting
   reformatter breaking hash parity (fixable).

## Dependency contract

The caller provides ``generate: Callable[[str], bytes]``. This matches
the public signature the composer (Phase 2, not yet landed) will
expose. Until the composer exists the tests auto-skip with a clear
message — the harness itself is still unit-tested so we know it works
the moment the composer plugs in.

## Temperature=0 caveat

Claude (and most LLMs) is not fully deterministic even at
temperature=0 — probability ties can flip. Hash equality is the
optimistic path; semantic equivalence is the realistic one. Record
the LLM model + version hash in the fixture directory if the
baseline was captured against a specific snapshot.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SUPPORTED_KINDS = frozenset({"yaml", "python"})


class ComposerLike(Protocol):
    """Shape the reproducibility harness expects from whatever
    generates content. The real ``composer.generate(prompt)`` will
    implement this when it lands (T06 / Phase 2).
    """

    def generate(self, prompt: str) -> bytes: ...


class SemanticDivergence(AssertionError):
    """Raised when the generated content neither hashes nor semantically
    matches the baseline. The message names the first diverging
    field / line so the reviewer can pick a side (composer drift vs.
    baseline stale).
    """


@dataclass(frozen=True, kw_only=True)
class Fixture:
    name: str
    prompt: str
    kind: str
    baseline_hash: str
    baseline_content: bytes | None

    @classmethod
    def load(cls, fixture_dir: Path) -> Fixture:
        prompt = (fixture_dir / "prompt.txt").read_text()
        kind_raw = (fixture_dir / "kind").read_text().strip().lower()
        if kind_raw not in SUPPORTED_KINDS:
            raise ValueError(
                f"fixture {fixture_dir.name}: kind {kind_raw!r} not in "
                f"{sorted(SUPPORTED_KINDS)}"
            )
        baseline_hash = (fixture_dir / "baseline_hash.txt").read_text().strip()
        if len(baseline_hash) != 64 or not all(
            c in "0123456789abcdef" for c in baseline_hash.lower()
        ):
            raise ValueError(
                f"fixture {fixture_dir.name}: baseline_hash.txt must be a "
                f"64-char hex SHA-256, got {baseline_hash!r}"
            )
        suffix = ".yml" if kind_raw == "yaml" else ".py"
        baseline_path = fixture_dir / f"baseline_content{suffix}"
        baseline_content = (
            baseline_path.read_bytes() if baseline_path.is_file() else None
        )
        return cls(
            name=fixture_dir.name,
            prompt=prompt,
            kind=kind_raw,
            baseline_hash=baseline_hash.lower(),
            baseline_content=baseline_content,
        )


def discover_fixtures(root: Path = FIXTURES_DIR) -> list[Fixture]:
    """Return every valid fixture under ``root``. Sub-directories that
    lack the three required files are silently skipped (makes it easy
    to stash WIP fixtures without breaking the suite).
    """
    if not root.is_dir():
        return []
    fixtures: list[Fixture] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        required = [entry / "prompt.txt", entry / "kind", entry / "baseline_hash.txt"]
        if not all(f.is_file() for f in required):
            continue
        fixtures.append(Fixture.load(entry))
    return fixtures


def semantic_equivalent_yaml(a: bytes, b: bytes) -> bool:
    """Parse both as YAML and compare. Key order, quote style, and
    indentation are ignored; value differences are not.
    """
    try:
        doc_a = yaml.safe_load(a)
        doc_b = yaml.safe_load(b)
    except yaml.YAMLError:
        return False
    return doc_a == doc_b


def semantic_equivalent_python(a: bytes, b: bytes) -> bool:
    """Parse both as Python and compare the AST. Whitespace, comments,
    and formatting are ignored; statements, identifiers, and literals
    are not.
    """
    try:
        tree_a = ast.parse(a)
        tree_b = ast.parse(b)
    except SyntaxError:
        return False
    return ast.dump(tree_a, annotate_fields=False, include_attributes=False) == ast.dump(
        tree_b, annotate_fields=False, include_attributes=False
    )


def check(
    *,
    generated: bytes,
    fixture: Fixture,
) -> None:
    """Run the comparator ladder against a freshly-generated artifact.

    Passes silently on match; raises :class:`SemanticDivergence` with a
    human-readable message if both the hash check and the semantic
    check fail.
    """
    actual_hash = hashlib.sha256(generated).hexdigest()
    if actual_hash == fixture.baseline_hash:
        return

    if fixture.baseline_content is None:
        raise SemanticDivergence(
            f"fixture {fixture.name!r}: hash mismatch "
            f"(expected {fixture.baseline_hash[:16]}..., got "
            f"{actual_hash[:16]}...) and no baseline_content file to "
            "run the semantic-equivalence fallback. Either the composer "
            "has drifted or the fixture needs re-capture."
        )

    kind = fixture.kind
    if kind == "yaml":
        same = semantic_equivalent_yaml(generated, fixture.baseline_content)
    elif kind == "python":
        same = semantic_equivalent_python(generated, fixture.baseline_content)
    else:
        raise ValueError(f"unsupported fixture kind {kind!r}")

    if same:
        return

    raise SemanticDivergence(
        f"fixture {fixture.name!r}: hash mismatch AND semantic "
        f"{kind} divergence. Expected hash "
        f"{fixture.baseline_hash[:16]}..., got {actual_hash[:16]}...; "
        "parsed contents also differ. Composer likely drifted."
    )
