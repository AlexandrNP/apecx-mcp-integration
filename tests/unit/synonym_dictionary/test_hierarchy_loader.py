"""Unit tests for hierarchy_loader — NCBI Taxonomy dump file parsers.

No SQLite, no network, no real taxdump.  All tests construct minimal
in-memory "files" via tmp_path to exercise the parsing logic.
"""

from __future__ import annotations

from pathlib import Path

from apecx_integration.synonym_dictionary.hierarchy_loader import (
    count_lines,
    parse_merged_dmp,
    parse_nodes_dmp,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_nodes(path: Path, rows: list[tuple[int, int, str]]) -> None:
    """Write a minimal nodes.dmp snippet.

    ``rows`` is a list of ``(tax_id, parent_tax_id, rank)`` tuples.
    Real nodes.dmp has many more columns but the parser only reads the first two.
    """
    with path.open("w", encoding="latin-1") as fh:
        for tax_id, parent_id, rank in rows:
            # Format: field0 \t|\t field1 \t|\t field2 \t|\n
            fh.write(f"{tax_id}\t|\t{parent_id}\t|\t{rank}\t|\n")


def _write_merged(path: Path, rows: list[tuple[int, int]]) -> None:
    """Write a minimal merged.dmp snippet."""
    with path.open("w", encoding="latin-1") as fh:
        for old_id, new_id in rows:
            fh.write(f"{old_id}\t|\t{new_id}\t|\n")


# ---------------------------------------------------------------------------
# parse_nodes_dmp
# ---------------------------------------------------------------------------


def test_parse_nodes_dmp_yields_child_parent_pairs(tmp_path: Path) -> None:
    nodes = tmp_path / "nodes.dmp"
    _write_nodes(nodes, [(37124, 11019, "species"), (11019, 11018, "genus")])
    result = list(parse_nodes_dmp(nodes))
    assert result == [(37124, 11019), (11019, 11018)]


def test_parse_nodes_dmp_root_self_loop_included(tmp_path: Path) -> None:
    """Root node (tax_id=1, parent=1) must be yielded — callers handle the self-loop."""
    nodes = tmp_path / "nodes.dmp"
    _write_nodes(nodes, [(1, 1, "no rank")])
    result = list(parse_nodes_dmp(nodes))
    assert result == [(1, 1)]


def test_parse_nodes_dmp_skips_malformed_lines(tmp_path: Path) -> None:
    nodes = tmp_path / "nodes.dmp"
    with nodes.open("w", encoding="latin-1") as fh:
        fh.write("not\ta\tvalid\tline\n")
        fh.write("37124\t|\t11019\t|\tspecies\t|\n")
        fh.write("abc\t|\t11019\t|\tspecies\t|\n")  # non-integer tax_id
    result = list(parse_nodes_dmp(nodes))
    assert result == [(37124, 11019)]


def test_parse_nodes_dmp_empty_file(tmp_path: Path) -> None:
    nodes = tmp_path / "nodes.dmp"
    nodes.write_text("", encoding="latin-1")
    assert list(parse_nodes_dmp(nodes)) == []


def test_parse_nodes_dmp_multiple_rows(tmp_path: Path) -> None:
    """Ensures streaming — all rows returned, order preserved."""
    nodes = tmp_path / "nodes.dmp"
    pairs = [(i, i - 1, "species") for i in range(100, 110)]
    _write_nodes(nodes, pairs)
    result = list(parse_nodes_dmp(nodes))
    assert len(result) == 10
    assert result[0] == (100, 99)
    assert result[-1] == (109, 108)


# ---------------------------------------------------------------------------
# parse_merged_dmp
# ---------------------------------------------------------------------------


def test_parse_merged_dmp_yields_old_new_pairs(tmp_path: Path) -> None:
    merged = tmp_path / "merged.dmp"
    _write_merged(merged, [(12345, 37124), (99999, 11021)])
    result = list(parse_merged_dmp(merged))
    assert result == [(12345, 37124), (99999, 11021)]


def test_parse_merged_dmp_skips_malformed_lines(tmp_path: Path) -> None:
    merged = tmp_path / "merged.dmp"
    with merged.open("w", encoding="latin-1") as fh:
        fh.write("garbage\n")
        fh.write("12345\t|\t37124\t|\n")
        fh.write("abc\t|\t37124\t|\n")
    result = list(parse_merged_dmp(merged))
    assert result == [(12345, 37124)]


def test_parse_merged_dmp_empty_file(tmp_path: Path) -> None:
    merged = tmp_path / "merged.dmp"
    merged.write_text("", encoding="latin-1")
    assert list(parse_merged_dmp(merged)) == []


# ---------------------------------------------------------------------------
# count_lines
# ---------------------------------------------------------------------------


def test_count_lines_matches_actual_line_count(tmp_path: Path) -> None:
    f = tmp_path / "sample.txt"
    f.write_text("a\nb\nc\n", encoding="latin-1")
    assert count_lines(f) == 3


def test_count_lines_empty_file_returns_zero(tmp_path: Path) -> None:
    f = tmp_path / "empty.txt"
    f.write_text("", encoding="latin-1")
    assert count_lines(f) == 0
