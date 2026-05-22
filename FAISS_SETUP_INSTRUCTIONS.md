# FAISS Index Setup (post-G81: OPTIONAL)

> **Status (2026-05-16)**: The domain RAG FAISS index is now **opt-in**.
> Most of the APECx stack runs without it; only synthesis workflows that
> wire the domain RAG branch need it. Workflows that wire the RAG branch
> degrade gracefully when the index is missing — they don't crash.
>
> If you specifically want the synthesis RAG branch, follow this guide.
> Otherwise, skip it — `apecx-setup` no longer builds the index by
> default.

## Decide whether you need it

**You DO need it if** any of the following is in your workflow:
* `SynthesisContextAssemblyStep` (typical query-answering pipeline).
* `UnlimitedSynthesisAssemblyStep`.
* `DomainRagSearchStep` (direct usage).

**You DON'T need it if** you're using:
* MCP database tools (`query_vaccines`, `query_pathogens`, etc.).
* The composer (workflow generation).
* The Rhea + HPC execution paths.
* The synonym dictionary or harvester.

## What you get without it

Workflows that wire the RAG branch will run, but:
* `rag_chunks` lists will be empty in every synthesis call.
* Logs carry one loud `RAG DISABLED` WARNING per process (subsequent
  RAG calls go to DEBUG to avoid log flooding).
* The MCP server prints a `RAG DISABLED` banner at startup.

## Building it

```bash
apecx-setup rag            # interactive, ~10 minutes
# or
apecx-setup --with-rag     # include in the full install chain
```

The build produces:

```
data/apecx_domain_rag/
├── faiss_index.bin        # ~4 MB domain index
└── metadata.json          # chunk metadata
```

## Verifying it

```bash
apecx-setup verify
```

`verify` checks for `data/apecx_domain_rag/faiss_index.bin`. When
present, the summary table reports `ok rag    index at <path>`.
When absent, it reports `skipped rag    opt-in — see docs/`.

You can also probe it from Python:

```python
from apecx_integration.agents.domain_rag import DomainRagIndex
idx = DomainRagIndex()
print(idx.is_available)   # True ⇒ ready to serve search() calls
```

`is_available` is a cheap stat probe — it does NOT load the FAISS
binary or the sentence-transformer model.

## Legacy `data/faiss_indexes/` — REMOVED 2026-05-22

The LFS-tracked `data/faiss_indexes/` directory was **deleted** 2026-05-22.
It was orphan (runtime reads `data/apecx_domain_rag/`) and its
`faiss_index.bin` LFS object had gone missing on the remote, which broke
`uv tool install git+...` for everyone (the LFS smudge filter runs during
the clone's `git reset --hard`). The repo is now LFS-free. Build the index
locally with `apecx-setup rag`; nothing reads `data/faiss_indexes/` anymore.
See `docs/no_github_data_2026-05-22.md`.

## Troubleshooting

### "FAISS index not found" but I built it

The build writes to `<workspace_root>/data/apecx_domain_rag/`. If
your `apecx-setup rag` ran from a different working directory, the
index may have landed elsewhere. Confirm with:

```bash
apecx-setup verify
```

or set `APECX_WORKSPACE_ROOT` explicitly:

```bash
export APECX_WORKSPACE_ROOT=/path/to/apecx-cowork
apecx-setup rag
```

### Synthesis returns empty results even with the index built

The MCP server probes the index at startup. If it was built AFTER
the server started, restart Claude Desktop (or the MCP host) so
the boot-time RAG-status banner re-runs and the index loader
picks up the new files.

### Build fails with "no space left on device"

`apecx-setup rag` writes a temporary intermediate corpus before
producing the final index. Free up ~2 GB and retry.
