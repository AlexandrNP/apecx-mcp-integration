# APECx Data Directory

This directory is the staging area for data files the APECx viral
immunology stack consumes. After running `apecx-setup`, the following
layout is populated:

```
data/
├── violin/                              # VIOLIN database CSVs (G82: Globus or gh release)
│   ├── Vaccine_Information.csv
│   ├── Pathogen_Information.csv
│   ├── Gene_Information.csv
│   ├── Vaccine_Pathogen_Information.csv
│   └── Gene_Vaccine_Pathogen_Information.csv
├── BVBRC_genome_alphavirus.csv          # BV-BRC alphavirus genome data
├── apecx_domain_rag/                    # OPTIONAL — domain RAG FAISS index (G81: opt-in)
│   ├── faiss_index.bin                  # ~4 MB domain index binary
│   └── metadata.json                    # index configuration + chunk metadata
└── faiss_indexes/                       # LEGACY mirror of apecx_domain_rag (LFS-tracked; G82 retires this)
    ├── faiss_index.bin
    ├── index.faiss
    ├── index.pkl
    └── metadata.json
```

## VIOLIN + BV-BRC dataset

The 6 CSV files at the top of the layout are **required** for every
non-RAG workflow path (database tools, composer, synthesis pipelines
that DON'T use the RAG branch, etc.).

Acquired by `apecx-setup data`. As of G82 (2026-05-16) the install
prefers a Globus transfer from the *APECx Data at Argonne LCF*
collection (path: `apecx-joshi-anl-general`); falls back to
`gh release download` from the `AlexandrNP/apecx-data` GitHub release
when Globus isn't configured. See `docs/globus_data_transfer.md`.

## Domain RAG FAISS index (OPTIONAL)

The `apecx_domain_rag/` directory holds the FAISS index that powers
the synthesis RAG branch. Build it with:

```bash
apecx-setup rag           # interactive, ~10 minutes
# or
apecx-setup --with-rag    # include in the full install chain
```

As of G81 (2026-05-16) RAG is **opt-in**:
* The default `apecx-setup` chain skips the FAISS build to keep
  first-time installs fast.
* Workflows that wire RAG branches (`SynthesisContextAssemblyStep`,
  `UnlimitedSynthesisAssemblyStep`, `DomainRagSearchStep`)
  **gracefully degrade** when the index is missing: they return
  empty `rag_chunks` lists and emit a single loud WARNING per
  process. Pipelines DO NOT crash.
* The MCP server prints a "RAG DISABLED" banner at startup when the
  index is missing, so operators see the issue in
  `~/Library/Logs/Claude/mcp-server-apecx.log` immediately rather
  than wondering why their synthesis results have empty RAG bundles.

## Legacy `faiss_indexes/` (LFS — being retired)

The `faiss_indexes/` mirror was committed via Git LFS in early
May 2026 as a bootstrap shortcut. Per G81 + G82, RAG is no longer
required for install success, so this LFS-tracked directory is
being phased out. The binaries are still in the LFS storage for any
historical commit that needs them, but new installs do NOT depend
on it — `apecx-setup rag` builds the index locally.

The G81 directive ("Skip FAISS index download — it should be
optional, required only for RAG workflows") motivates this shift.
A future cleanup commit will `git rm --cached` the LFS pointers and
update `.gitignore` so the directory stops being tracked at all.

## Related data sources (not in this directory)

- Synonym dictionary SQLite (`$APECX_SYNONYM_DICT_PATH` or
  `~/.apecx/synonyms.db`) — built lazily by the MCP server.
- PubMed harvest cache (`$APECX_HARVEST_CACHE` or
  `~/.apecx/harvest/`) — populated on first use by the harvester
  sink.
- Composer's component-retrieval RAG index
  (`<config_dir>/rag_index/`) — built by `scripts/build_rag_index.py`;
  much smaller than the domain RAG index, and independent of it.
