"""Globus Search index client for the APECx harvested-corpus.

The harvesters (in the ``apecx-harvesters`` sibling repo) run as a
stand-alone process: they harvest from PubMed / PDB / DataCite and
write to two outputs — the **APECx synonym dictionary** (consumed by
``apecx_integration.synonym_dictionary``) and the **Globus Search
index** (consumed via this module).

This module is the integration seam at the **ingest** boundary: it
queries the search index but never writes to it. Harvester code is
explicitly out of scope here per the workspace policy (2026-05-05
user directive: "Think of harvesters as a stand-alone process that
will run once and update search index, as well as generate APECx
synonyms dictionary.").

Public API: :func:`search` returns a list of normalized hit dicts
shaped like ``{subject, score, content}`` for the synthesis pipeline
and the standalone MCP tool.
"""

from apecx_integration.agents.globus_search.client import (
    APECX_GLOBUS_INDEX_UUID,
    GlobusSearchUnavailableError,
    search,
)

__all__ = [
    "APECX_GLOBUS_INDEX_UUID",
    "GlobusSearchUnavailableError",
    "search",
]
