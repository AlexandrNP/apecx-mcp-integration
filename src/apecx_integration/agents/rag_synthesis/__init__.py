"""rag_synthesis — LLM synthesis with retrieved RAG chunks +
structured DB context.

Builds a free-form Markdown response with inline citations from:

  - User query (free-text scientist question).
  - Retrieved RAG semantic chunks (via
    ``nanobrain.lightweight.component_index.ComponentIndex`` or
    a caller-supplied list of ``(chunk_id, text)`` pairs).
  - Structured BV-BRC genome data (list of ``GenomeData``-shaped
    dicts with id, name, taxonomy, lineage when available).
  - Structured VIOLIN cached mappings (``query_term ->
    canonical_term`` with optional confidence + source pointer).
  - Optional harvester publication metadata (DataCite-shaped
    records: title, authors, doi, year, abstract).

Output: a Markdown string with inline citations of the form
``[BV-BRC genome 11036.7]``, ``[VIOLIN vaccine VO_0000001]``,
``[RAG chunk #4]``, ``[10.1234/abc]``. The synthesis prompt is
loaded from a YAML config (operator-tunable). The LLM client is
the same ``_build_chat_llm`` factory used elsewhere — local-LLM
deployments work out of the box (Ollama / vLLM via OpenAI-compat
endpoint).

Migrated from the apecx-rag prototype's LangGraph agent team
(2026-04-27, user directive). The simpler synchronous shape ships
first; the multi-step query→search→summarize→critic agent loop
is a Phase-2 follow-up.

Public API:
  - ``synthesize_response(query, *, rag_chunks, bvbrc_genomes,
    violin_mappings, publications=None, llm=None,
    config=None) -> str``
  - ``SynthesisConfig`` — Pydantic schema for the synthesis
    config (system_prompt, max_chunks, etc.).
  - ``DEFAULT_SYNTHESIS_CONFIG_PATH`` — bundled YAML config path.
"""

from apecx_integration.agents.rag_synthesis.harvester_adapter import (
    datacite_to_publication,
)
from apecx_integration.agents.rag_synthesis.synthesizer import (
    DEFAULT_SYNTHESIS_CONFIG_PATH,
    SynthesisConfig,
    synthesize_response,
)

__all__ = [
    "DEFAULT_SYNTHESIS_CONFIG_PATH",
    "SynthesisConfig",
    "datacite_to_publication",
    "synthesize_response",
]
