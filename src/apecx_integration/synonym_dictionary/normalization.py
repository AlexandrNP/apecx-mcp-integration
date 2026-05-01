"""Surface-form normalization shared by Stage 1 (build-time inverse index)
and Stage 2 (runtime user-input lookup).

Both sides MUST use this same function — divergence here is silent
breakage of the lookup path.  Per the contract doc §5.
"""

from __future__ import annotations

import re
import unicodedata

_WHITESPACE_RUN = re.compile(r"\s+")
# Strip surrounding parens / brackets / quotes but leave internal punctuation
# (hyphens, dots, slashes) intact — those tend to be load-bearing in
# scientific labels (e.g. "Influenza A virus (A/Brisbane/02/2018(H1N1))").
_SURROUND_PUNCT = re.compile(r"^[\s()\[\]{}\"'`]+|[\s()\[\]{}\"'`]+$")


def normalize_surface_form(s: str) -> str:
    """Canonicalize a surface form for lookup-key purposes.

    Steps:

    1. Unicode NFKC compose.
    2. ``str.casefold()`` — handles non-ASCII case (e.g. Greek beta, German ß).
    3. Strip surrounding whitespace + brackets + quotes.
    4. Collapse runs of internal whitespace to a single space.

    Idempotent: ``normalize(normalize(s)) == normalize(s)``.
    """
    if not s:
        return ""
    nfkc = unicodedata.normalize("NFKC", s)
    folded = nfkc.casefold()
    stripped = _SURROUND_PUNCT.sub("", folded)
    collapsed = _WHITESPACE_RUN.sub(" ", stripped).strip()
    return collapsed
