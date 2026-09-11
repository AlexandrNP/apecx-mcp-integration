"""Literature-agent package — deterministic taxon tagging of abstracts for a
paper-coverage view of the synonym dictionary.

Thin, dependency-light layers (stdlib + the lazy synonym normalizer at import
time — NO faiss / sentence-transformers / globus_sdk at import, per the
clean-install rule; those load lazily inside the functions that use them):

  - ``gazetteer``   — surface-form → NCBITaxon IRI matcher (``build_gazetteer``,
    ``Gazetteer``, ``Tag``); ``build_from_dictionary`` streams the real
    dictionary.sqlite inverse_index.
  - ``coverage``    — aggregate matched surfaces across a corpus
    (``build_coverage``; ``harvest_and_build`` adds a live-PubMed harvest).
  - ``stamped_corpus`` — stamp each abstract with its matched IRIs + an
    in-memory, IRI-filterable store (``stamp_abstract``, ``StampedCorpus``);
    ``build_faiss_subindex`` builds a per-query FAISS index.

The real inputs (dictionary inverse_index, live PubMed harvest, FAISS
sub-index) are implemented and real-data-verified; the pure aggregation layers
stay injectable for tests. Sibling modules ``resolve``, ``harvest``,
``pipeline``, and ``ablation`` build the organism-filter pipeline on top.
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
