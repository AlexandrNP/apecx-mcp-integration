# APECx Data Directory

This directory is the staging area for data files the APECx viral
immunology stack consumes. After running `apecx-setup`, the following
layout is populated:

```
data/
├── violin/                              # VIOLIN database CSVs (Globus only)
│   ├── Vaccine_Information.csv
│   ├── Pathogen_Information.csv
│   ├── Gene_Information.csv
│   ├── Vaccine_Pathogen_Information.csv
│   └── Gene_Vaccine_Pathogen_Information.csv
├── BVBRC_genome_alphavirus.csv          # BV-BRC alphavirus genome data (Globus only)
└── apecx_domain_rag/                    # OPTIONAL — domain RAG FAISS index (G81: opt-in)
    ├── faiss_index.bin                  # ~4 MB domain index binary
    └── metadata.json                    # index configuration + chunk metadata
```

Everything under `data/` except this README is **acquired or built at
setup time, never committed.** No data ships in the repo or via GitHub —
datasets come over Globus (`apecx-setup data`) and the RAG index is built
locally (`apecx-setup rag`). The whole directory is git-ignored except
`README.md`.

## VIOLIN + BV-BRC dataset

The 6 CSV files at the top of the layout are **required** for every
non-RAG workflow path (database tools, composer, synthesis pipelines
that DON'T use the RAG branch, etc.).

Acquired by `apecx-setup data` via a Globus transfer — the **sole**
data path. The `gh release download` fallback was retired 2026-05-21
(G127); there is no GitHub data download. BV-BRC is required; VIOLIN is
optional (gated by the `apecx-project-all` Globus Group). Default auth is
the web/native thick client (`apecx-globus-setup login`); headless/CI uses
the thin-client secret path (`APECX_GLOBUS_AUTH_MODE=client_credentials`).
See `docs/globus_data_transfer.md`.

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

## Legacy `faiss_indexes/` — REMOVED 2026-05-22

The LFS-tracked `data/faiss_indexes/` mirror (committed early May 2026 as a
bootstrap shortcut) was **deleted** 2026-05-22. It was orphan — runtime reads
`data/apecx_domain_rag/`, not this path — and ~685 MB of LFS binaries whose
`faiss_index.bin` LFS object went missing on the remote, which **broke
`uv tool install git+...`** (the smudge filter runs during the clone's
`git reset --hard`). The repo is now LFS-free; `.gitattributes` carries no
LFS rules and `.gitignore` blocks re-committing index artifacts. Build the
index locally with `apecx-setup rag`. See `docs/no_github_data_2026-05-22.md`.

## Related data sources (not in this directory)

- Synonym dictionary SQLite (`$APECX_SYNONYM_DICT_PATH` or
  `~/.apecx/synonyms.db`) — built lazily by the MCP server.
- PubMed harvest cache (`$APECX_HARVEST_CACHE` or
  `~/.apecx/harvest/`) — populated on first use by the harvester
  sink.
- Composer's component-retrieval RAG index
  (`<config_dir>/rag_index/`) — built by `scripts/build_rag_index.py`;
  much smaller than the domain RAG index, and independent of it.
