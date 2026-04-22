"""Unit tests of the reproducibility harness itself.

These don't run any fixtures — they verify the comparator ladder
(hash equality, YAML/Python semantic equivalence) and the fixture
loader's error shapes. Runs on every commit; no composer or LLM
required. Marker: unit (default).
"""

from __future__ import annotations

import hashlib
import textwrap
from pathlib import Path

import pytest

from tests.reproducibility.harness import (
    Fixture,
    SemanticDivergence,
    check,
    discover_fixtures,
    semantic_equivalent_python,
    semantic_equivalent_yaml,
)


def _write_fixture(
    dir_: Path,
    name: str,
    *,
    prompt: str,
    kind: str,
    baseline_bytes: bytes,
    include_baseline_content: bool = True,
) -> Path:
    fx = dir_ / name
    fx.mkdir()
    (fx / "prompt.txt").write_text(prompt)
    (fx / "kind").write_text(kind)
    (fx / "baseline_hash.txt").write_text(hashlib.sha256(baseline_bytes).hexdigest())
    if include_baseline_content:
        suffix = ".yml" if kind == "yaml" else ".py"
        (fx / f"baseline_content{suffix}").write_bytes(baseline_bytes)
    return fx


# ---- YAML semantic equivalence ----------------------------------------


def test_yaml_semantic_equivalence_ignores_key_order() -> None:
    a = b"a: 1\nb: 2\n"
    b = b"b: 2\na: 1\n"
    assert semantic_equivalent_yaml(a, b)


def test_yaml_semantic_equivalence_ignores_indentation_style() -> None:
    a = b"steps:\n  - name: one\n  - name: two\n"
    b = b"steps:\n- name: one\n- name: two\n"
    assert semantic_equivalent_yaml(a, b)


def test_yaml_semantic_equivalence_catches_value_drift() -> None:
    a = b"threshold: 0.92\n"
    b = b"threshold: 0.80\n"
    assert not semantic_equivalent_yaml(a, b)


def test_yaml_semantic_equivalence_rejects_invalid_yaml() -> None:
    assert not semantic_equivalent_yaml(b"{: : :", b"{: : :")


# ---- Python semantic equivalence --------------------------------------


def test_python_ast_equivalence_ignores_whitespace_and_comments() -> None:
    a = b"def f(x): return x+1\n"
    b = b"def f(x):\n    # add one\n    return  x + 1\n"
    assert semantic_equivalent_python(a, b)


def test_python_ast_equivalence_catches_literal_drift() -> None:
    a = b"THRESHOLD = 0.92\n"
    b = b"THRESHOLD = 0.80\n"
    assert not semantic_equivalent_python(a, b)


def test_python_ast_equivalence_rejects_syntax_errors() -> None:
    assert not semantic_equivalent_python(b"def bad(", b"def bad(")


# ---- Fixture loader ---------------------------------------------------


def test_discover_fixtures_skips_dirs_missing_required_files(tmp_path) -> None:
    _write_fixture(
        tmp_path, "ok_fixture",
        prompt="generate x", kind="yaml",
        baseline_bytes=b"a: 1\n",
    )
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    (incomplete / "prompt.txt").write_text("only this file")

    found = discover_fixtures(tmp_path)
    assert [f.name for f in found] == ["ok_fixture"]


def test_fixture_load_rejects_unknown_kind(tmp_path) -> None:
    fx = tmp_path / "bad_kind"
    fx.mkdir()
    (fx / "prompt.txt").write_text("x")
    (fx / "kind").write_text("json")
    (fx / "baseline_hash.txt").write_text("0" * 64)
    with pytest.raises(ValueError, match="kind 'json' not in"):
        Fixture.load(fx)


def test_fixture_load_rejects_non_hex_baseline_hash(tmp_path) -> None:
    fx = tmp_path / "bad_hash"
    fx.mkdir()
    (fx / "prompt.txt").write_text("x")
    (fx / "kind").write_text("yaml")
    (fx / "baseline_hash.txt").write_text("not-a-hex-string-of-the-right-length")
    with pytest.raises(ValueError, match="baseline_hash.txt must be a 64-char hex"):
        Fixture.load(fx)


# ---- Comparator ladder -------------------------------------------------


def test_check_passes_on_hash_equality(tmp_path) -> None:
    content = b"workflow:\n  steps: []\n"
    _write_fixture(tmp_path, "ok", prompt="x", kind="yaml", baseline_bytes=content)
    fixture = Fixture.load(tmp_path / "ok")
    check(generated=content, fixture=fixture)  # must not raise


def test_check_falls_back_to_semantic_when_hash_differs_but_yaml_equivalent(
    tmp_path,
) -> None:
    baseline = b"a: 1\nb: 2\n"
    regenerated = b"b: 2\na: 1\n"  # same YAML, different bytes
    _write_fixture(
        tmp_path, "reorder", prompt="x", kind="yaml", baseline_bytes=baseline,
    )
    fixture = Fixture.load(tmp_path / "reorder")
    check(generated=regenerated, fixture=fixture)  # must not raise


def test_check_raises_when_hash_and_semantic_both_differ(tmp_path) -> None:
    baseline = b"threshold: 0.92\n"
    drifted = b"threshold: 0.80\n"
    _write_fixture(
        tmp_path, "drift", prompt="x", kind="yaml", baseline_bytes=baseline,
    )
    fixture = Fixture.load(tmp_path / "drift")
    with pytest.raises(SemanticDivergence, match="semantic yaml divergence"):
        check(generated=drifted, fixture=fixture)


def test_check_raises_when_hash_differs_and_no_baseline_content_file(
    tmp_path,
) -> None:
    baseline = b"a: 1\n"
    regen = b"b: 2\n"
    _write_fixture(
        tmp_path, "no_fallback",
        prompt="x", kind="yaml", baseline_bytes=baseline,
        include_baseline_content=False,  # only hash file, no baseline_content.yml
    )
    fixture = Fixture.load(tmp_path / "no_fallback")
    with pytest.raises(SemanticDivergence, match="no baseline_content file"):
        check(generated=regen, fixture=fixture)


def test_check_python_semantic_fallback(tmp_path) -> None:
    baseline = textwrap.dedent(
        """\
        def f(x):
            return x + 1
        """
    ).encode()
    regenerated = b"def f(x): return x+1\n"
    _write_fixture(
        tmp_path, "py_reformat", prompt="x", kind="python", baseline_bytes=baseline,
    )
    fixture = Fixture.load(tmp_path / "py_reformat")
    check(generated=regenerated, fixture=fixture)  # must not raise
