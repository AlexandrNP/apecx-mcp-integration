"""literature_rag — ontology-filtered literature RAG workflow.

Two entry points:
  - ``literature_rag_workflow.yml`` (Workflow.from_config): the pre-stamped-records
    cascade — resolve organism → NCBITaxon IRI, filter a supplied ``stamped_records``
    set to that IRI, then answer the question over the filtered set with cited PMIDs.
  - ``builder.build_literature_rag_workflow`` (lightweight, the MCP catalog tool):
    prepends a ``HarvestStampStep`` so the workflow runs from a bare
    ``{organism, question}`` request — it harvests PubMed and stamps the records
    itself, then runs the same resolve → filter → answer cascade.

All steps carry real implementations (no stubs) and degrade loud (an unresolved
organism / empty filter / synthesis failure yields a well-formed non-"ok" status,
never a crash). In desktop locus the answer step hands the ontology-filtered
evidence to the host LLM rather than calling the apecx LLM.
"""
