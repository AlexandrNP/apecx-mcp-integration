"""Literature-agent package (skeleton) — deterministic taxon tagging of
abstracts for a paper-coverage view of the synonym dictionary.

Three thin, dependency-free layers (stdlib + the lazy synonym normalizer
only — NO faiss / sentence-transformers / globus_sdk at import time, per the
clean-install rule):

  - ``gazetteer``   — surface-form → NCBITaxon IRI matcher (``build_gazetteer``,
    ``Gazetteer``, ``Tag``).
  - ``coverage``    — aggregate matched surfaces across a corpus
    (``build_coverage``).
  - ``stamped_corpus`` — stamp each abstract with its matched IRIs + an
    in-memory, IRI-filterable store (``stamp_abstract``, ``StampedCorpus``).

The real inputs (dictionary.sqlite inverse_index, Globus/PubMed harvest,
FAISS sub-index) are documented ``NotImplementedError`` stubs — this skeleton
is exercised only by injected in-memory data.
"""

from apecx_integration.agents.literature.coverage import build_coverage
from apecx_integration.agents.literature.gazetteer import (
    Gazetteer,
    Tag,
    build_gazetteer,
)
from apecx_integration.agents.literature.stamped_corpus import (
    StampedCorpus,
    stamp_abstract,
)

__all__ = [
    "Gazetteer",
    "Tag",
    "build_gazetteer",
    "build_coverage",
    "StampedCorpus",
    "stamp_abstract",
]
