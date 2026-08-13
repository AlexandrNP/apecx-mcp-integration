"""Unit tests for the shared BV-BRC RQL query-value safety helper.

`rql_safe_name` strips parenthetical segments (and any stray parens) that break BV-BRC RQL:
BV-BRC percent-decodes the value then RQL-parses it, so a literal '(' inside eq(field,value)
returns HTTP 400. Shared by the taxonomy and protein-FASTA steps. Real-BV-BRC parity for each
consumer lives in their integration tests.
"""

from __future__ import annotations

import pytest

from apecx_integration.composition.steps._bvbrc_rql import rql_safe_name


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Zika virus (ZIKV)", "Zika virus"),
        ("Foo (a) Bar (b)", "Foo Bar"),
        ("no parens", "no parens"),
        ("(ZIKV)", ""),
        ("Powassan   virus", "Powassan virus"),  # internal-whitespace collapse
        ("Zika virus (ZIKV) ", "Zika virus"),  # trailing space after strip
        ("Foo (bar (baz))", "Foo"),  # nested: greedy inner strip + residual paren drop
        ("Foo (bar", "Foo bar"),  # unbalanced open: stray '(' dropped, no residual delimiter
        ("envelope glycoprotein (E1)", "envelope glycoprotein"),  # protein-product gloss
    ],
)
def test_rql_safe_name_strips_parenthetical(raw, expected):
    assert rql_safe_name(raw) == expected
