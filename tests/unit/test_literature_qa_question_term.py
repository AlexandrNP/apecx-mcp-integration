"""Unit tests for ``literature_qa._question_term`` (pure; no network).

The real-PubMed counterpart is ``tests/integration/test_literature_qa_question_pool_real.py``,
which runs the same A1 term through eSearch and asserts the on-topic PMID is retrieved.
"""

from __future__ import annotations

from apecx_integration.mcp_surface.tools.literature_qa import _question_term

A1_QUESTION = (
    "List the PubMed IDs of studies reporting Ross River virus E2 glycoprotein epitopes "
    "recognised by neutralising antibodies, with the PMID for each."
)


def test_a1_question_yields_organism_anchored_facet_term():
    assert _question_term("Ross River virus", A1_QUESTION) == (
        '"Ross River virus"[Title/Abstract] AND '
        "(E2 OR glycoprotein OR epitopes OR recognised OR neutralising OR antibodies)"
    )


def test_generic_and_organism_words_are_not_facets():
    term = _question_term("Ross River virus", "List the PubMed IDs of studies on Ross River virus")
    assert term == "Ross River virus"


def test_uppercase_boolean_word_is_not_a_facet():
    term = _question_term("Mayaro virus", "E2 epitopes NOT neutralising")
    assert term == '"Mayaro virus"[Title/Abstract] AND (E2 OR epitopes OR neutralising)'


def test_quotes_in_organism_are_stripped():
    term = _question_term('H5N1 "avian" flu', "HA epitopes")
    assert term == '"H5N1 avian flu"[Title/Abstract] AND (HA OR epitopes)'


def test_facets_are_deduplicated_case_insensitively():
    term = _question_term("Mayaro virus", "E2 epitopes and e2 epitopes")
    assert term == '"Mayaro virus"[Title/Abstract] AND (E2 OR epitopes)'


def test_short_tokens_other_than_acronyms_are_dropped():
    term = _question_term("Chikungunya virus", "a nsP2 protease in it")
    assert term == '"Chikungunya virus"[Title/Abstract] AND (nsP2 OR protease)'
