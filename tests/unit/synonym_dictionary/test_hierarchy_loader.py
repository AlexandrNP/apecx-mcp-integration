"""Unit tests for hierarchy_loader — NCBI Taxonomy dump file parsers.

No SQLite, no network, no real taxdump.  All tests construct minimal
in-memory "files" via tmp_path to exercise the parsing logic.
"""

from __future__ import annotations

from pathlib import Path

from apecx_integration.synonym_dictionary.hierarchy_loader import (
    NCBI_NAME_CLASSES_INGESTED,
    NameRecord,
    compute_species_ancestors,
    compute_subtree_descendants,
    count_lines,
    parse_delnodes_dmp,
    parse_merged_dmp,
    parse_names_dmp,
    parse_nodes_dmp,
    parse_nodes_dmp_with_rank,
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


def _write_names(path: Path, rows: list[tuple[int, str, str, str]]) -> None:
    """Write a minimal names.dmp snippet.

    ``rows`` is a list of ``(tax_id, name_text, unique_name, name_class)``.
    Format: ``<tax_id>\\t|\\t<name>\\t|\\t<unique>\\t|\\t<class>\\t|\\n``.
    """
    with path.open("w", encoding="latin-1") as fh:
        for tax_id, name, unique, klass in rows:
            fh.write(f"{tax_id}\t|\t{name}\t|\t{unique}\t|\t{klass}\t|\n")


def _write_delnodes(path: Path, ids: list[int]) -> None:
    """Write a minimal delnodes.dmp snippet."""
    with path.open("w", encoding="latin-1") as fh:
        for tax_id in ids:
            fh.write(f"{tax_id}\t|\n")


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


# ---------------------------------------------------------------------------
# parse_names_dmp (SC-A3)
# ---------------------------------------------------------------------------


def test_parse_names_dmp_yields_all_columns(tmp_path: Path) -> None:
    names = tmp_path / "names.dmp"
    _write_names(
        names,
        [
            (11036, "Eastern equine encephalitis virus", "", "scientific name"),
            (11036, "EEEV", "", "acronym"),
            (11036, "Eastern equine encephalomyelitis virus", "", "equivalent name"),
        ],
    )
    result = list(parse_names_dmp(names))
    assert result == [
        NameRecord(11036, "Eastern equine encephalitis virus", "", "scientific name"),
        NameRecord(11036, "EEEV", "", "acronym"),
        NameRecord(11036, "Eastern equine encephalomyelitis virus", "", "equivalent name"),
    ]


def test_parse_names_dmp_handles_unique_name_column(tmp_path: Path) -> None:
    """NCBI fills unique_name when name_text collides across taxa."""
    names = tmp_path / "names.dmp"
    _write_names(names, [(1396, "Bacillus", "Bacillus <bacterium>", "scientific name")])
    result = list(parse_names_dmp(names))
    assert result == [NameRecord(1396, "Bacillus", "Bacillus <bacterium>", "scientific name")]


def test_parse_names_dmp_skips_malformed_lines(tmp_path: Path) -> None:
    names = tmp_path / "names.dmp"
    with names.open("w", encoding="latin-1") as fh:
        fh.write("garbage\n")  # not enough columns
        fh.write("11036\t|\tEEEV\t|\t\t|\tacronym\t|\n")
        fh.write("abc\t|\tnonsense\t|\t\t|\tsynonym\t|\n")  # non-integer tax_id
        fh.write("11036\t|\t\t|\t\t|\tsynonym\t|\n")  # empty name_text
    result = list(parse_names_dmp(names))
    assert result == [NameRecord(11036, "EEEV", "", "acronym")]


def test_parse_names_dmp_empty_file(tmp_path: Path) -> None:
    names = tmp_path / "names.dmp"
    names.write_text("", encoding="latin-1")
    assert list(parse_names_dmp(names)) == []


def test_ingested_name_classes_match_design_doc() -> None:
    """SC-A4 / D10 of SYNONYM_COMPLETENESS_PLAN — exactly these 7 are
    ingested. Adding others is a coverage-policy change requiring a
    plan update."""
    assert (
        frozenset(
            {
                "scientific name",
                "synonym",
                "equivalent name",
                "common name",
                "genbank common name",
                "acronym",
                "blast name",
            }
        )
        == NCBI_NAME_CLASSES_INGESTED
    )


# ---------------------------------------------------------------------------
# parse_delnodes_dmp (SC-A3)
# ---------------------------------------------------------------------------


def test_parse_delnodes_dmp_yields_taxon_ids(tmp_path: Path) -> None:
    delnodes = tmp_path / "delnodes.dmp"
    _write_delnodes(delnodes, [12345, 67890, 11111])
    result = list(parse_delnodes_dmp(delnodes))
    assert result == [12345, 67890, 11111]


def test_parse_delnodes_dmp_skips_non_integer_lines(tmp_path: Path) -> None:
    delnodes = tmp_path / "delnodes.dmp"
    with delnodes.open("w", encoding="latin-1") as fh:
        fh.write("12345\t|\n")
        fh.write("not_a_number\t|\n")
        fh.write("\t|\n")  # empty
        fh.write("67890\t|\n")
    result = list(parse_delnodes_dmp(delnodes))
    assert result == [12345, 67890]


def test_parse_delnodes_dmp_empty_file(tmp_path: Path) -> None:
    delnodes = tmp_path / "delnodes.dmp"
    delnodes.write_text("", encoding="latin-1")
    assert list(parse_delnodes_dmp(delnodes)) == []


# ---------------------------------------------------------------------------
# compute_subtree_descendants (SC-A3)
# ---------------------------------------------------------------------------


def test_compute_subtree_descendants_root_only_when_no_children(tmp_path: Path) -> None:
    """A leaf root returns just itself."""
    nodes = tmp_path / "nodes.dmp"
    _write_nodes(nodes, [(11036, 11034, "species")])  # 11036 has no children
    assert compute_subtree_descendants(nodes, 11036) == {11036}


def test_compute_subtree_descendants_finds_full_subtree(tmp_path: Path) -> None:
    """The virus subtree shape used by the production build (root 10239)
    is mirrored here with a hand-built mini-tree."""
    nodes = tmp_path / "nodes.dmp"
    # 10239 (root)
    #  ├── 11018 (genus Alphavirus)
    #  │    ├── 11036 (species EEEV)
    #  │    └── 37124 (species CHIKV)
    #  └── 99999 (some other clade)
    _write_nodes(
        nodes,
        [
            (10239, 1, "superkingdom"),  # parent is root-of-life, outside subtree
            (11018, 10239, "genus"),
            (11036, 11018, "species"),
            (37124, 11018, "species"),
            (99999, 10239, "family"),
            (12345, 50000, "species"),  # in different subtree entirely
        ],
    )
    descendants = compute_subtree_descendants(nodes, 10239)
    assert descendants == {10239, 11018, 11036, 37124, 99999}
    # The unrelated clade rooted at 50000 is excluded.
    assert 12345 not in descendants


def test_compute_subtree_descendants_handles_root_self_loop(tmp_path: Path) -> None:
    """NCBI's root has parent == self; the BFS must terminate."""
    nodes = tmp_path / "nodes.dmp"
    _write_nodes(
        nodes,
        [
            (1, 1, "no rank"),  # the self-loop
            (10239, 1, "superkingdom"),
            (11018, 10239, "genus"),
        ],
    )
    descendants = compute_subtree_descendants(nodes, 1)
    assert descendants == {1, 10239, 11018}


def test_compute_subtree_descendants_missing_root_yields_root_only(
    tmp_path: Path,
) -> None:
    """If the root id has no children in the hierarchy, the result is
    just the root (no fabricated descendants)."""
    nodes = tmp_path / "nodes.dmp"
    _write_nodes(nodes, [(11018, 10239, "genus")])
    # root 99999 doesn't appear anywhere in nodes.dmp
    assert compute_subtree_descendants(nodes, 99999) == {99999}


# ---------------------------------------------------------------------------
# parse_nodes_dmp_with_rank + compute_species_ancestors (strain→species)
# ---------------------------------------------------------------------------


def test_parse_nodes_dmp_with_rank_yields_rank(tmp_path: Path) -> None:
    nodes = tmp_path / "nodes.dmp"
    _write_nodes(
        nodes,
        [
            (1, 1, "no rank"),
            (1773, 1763, "species"),
            (83332, 1773, "strain"),
        ],
    )
    rows = list(parse_nodes_dmp_with_rank(nodes))
    assert rows == [
        (1, 1, "no rank"),
        (1773, 1763, "species"),
        (83332, 1773, "strain"),
    ]


def test_parse_nodes_dmp_with_rank_skips_missing_rank_column(tmp_path: Path) -> None:
    """A line with fewer than 3 columns is skipped (no rank to read)."""
    nodes = tmp_path / "nodes.dmp"
    with nodes.open("w", encoding="latin-1") as fh:
        fh.write("5\t|\t6\t|\n")  # only two columns
        fh.write("7\t|\t8\t|\tgenus\t|\n")
    rows = list(parse_nodes_dmp_with_rank(nodes))
    assert rows == [(7, 8, "genus")]


def _mtb_hierarchy(path: Path) -> None:
    """Minimal NCBI-shaped fragment: root → genus → species → strain,
    plus a sub-strain to exercise the multi-hop walk-up and memoization."""
    _write_nodes(
        path,
        [
            (1, 1, "no rank"),  # root self-loop
            (1763, 1, "genus"),  # Mycobacterium
            (1773, 1763, "species"),  # M. tuberculosis
            (83332, 1773, "strain"),  # H37Rv
            (999001, 83332, "no rank"),  # a sub-strain isolate
        ],
    )


def test_compute_species_ancestors_strain_maps_to_species(tmp_path: Path) -> None:
    nodes = tmp_path / "nodes.dmp"
    _mtb_hierarchy(nodes)
    species_map = compute_species_ancestors(nodes)
    # strain and sub-strain both normalize up to the species 1773
    assert species_map[83332] == 1773
    assert species_map[999001] == 1773


def test_compute_species_ancestors_species_maps_to_itself(tmp_path: Path) -> None:
    nodes = tmp_path / "nodes.dmp"
    _mtb_hierarchy(nodes)
    species_map = compute_species_ancestors(nodes)
    assert species_map[1773] == 1773


def test_compute_species_ancestors_omits_taxa_above_species(tmp_path: Path) -> None:
    """Genus / root have no species ancestor and are omitted from the map."""
    nodes = tmp_path / "nodes.dmp"
    _mtb_hierarchy(nodes)
    species_map = compute_species_ancestors(nodes)
    assert 1763 not in species_map  # genus
    assert 1 not in species_map  # root


def test_compute_species_ancestors_custom_rank(tmp_path: Path) -> None:
    """species_rank is configurable — normalising to genus walks one level up."""
    nodes = tmp_path / "nodes.dmp"
    _mtb_hierarchy(nodes)
    genus_map = compute_species_ancestors(nodes, species_rank="genus")
    assert genus_map[83332] == 1763
    assert genus_map[1773] == 1763
    assert genus_map[1763] == 1763  # genus maps to itself
    assert 1 not in genus_map  # root is above genus


def test_compute_species_ancestors_self_loop_terminates(tmp_path: Path) -> None:
    """A taxon whose only ancestor is the root self-loop has no species."""
    nodes = tmp_path / "nodes.dmp"
    _write_nodes(nodes, [(1, 1, "no rank"), (2, 1, "superkingdom")])
    species_map = compute_species_ancestors(nodes)
    assert species_map == {}
