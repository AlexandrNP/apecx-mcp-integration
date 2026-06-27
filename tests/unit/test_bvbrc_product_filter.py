"""Unit test for the BV-BRC substitution junk-product filter (2026-06-27 HSV probe).

The too-few-sequences fallback must NOT substitute the conservation analysis to a generic catch-all
product name (e.g. taxon 126283 "Herpes simplex virus unknown type" → "unnamed protein product").
``_is_informative_product`` gates the substitution candidates; this pins which names are rejected.
"""

from __future__ import annotations

import pytest

from apecx_integration.composition.steps.bvbrc_protein_fasta_step import _is_informative_product


@pytest.mark.parametrize(
    "product",
    [
        "unnamed protein product",
        "hypothetical protein",
        "uncharacterized protein",
        "uncharacterised protein",
        "unknown protein",
        "predicted protein",
        "putative protein",
        "protein",
        "product",
        "  ",
        "",
    ],
)
def test_generic_catchall_products_are_rejected(product):
    assert _is_informative_product(product) is False


@pytest.mark.parametrize(
    "product",
    [
        "neuraminidase",
        "surface glycoprotein",
        "thymidine kinase",
        "envelope glycoprotein E1",
        "putative ORF1ab polyprotein",  # named-but-putative is still informative
        "nonstructural protein NS3",
        "main protease",
    ],
)
def test_specific_named_products_are_kept(product):
    assert _is_informative_product(product) is True
