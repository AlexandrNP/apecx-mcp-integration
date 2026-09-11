"""literature_rag — ontology-filtered literature RAG workflow (SKELETON).

Two-step pipeline scaffold: an ontology filter narrows a candidate record set,
then a literature-RAG step answers a query grounded in the filtered records.

Both steps are DEGRADE-LOUD STUBS in this skeleton — they log a clear
"not yet implemented" line and return a well-formed empty envelope. The real
retrieval / RAG logic (literature agents, Globus client) lands in a later
branch; this package intentionally imports NONE of it.
"""
