# APECX Data Directory

This directory contains essential data files for the APECX viral immunology analysis system.

## FAISS Indexes

The `faiss_indexes/` directory contains pre-built vector search indexes for the RAG (Retrieval-Augmented Generation) component:

### Files:
- **`faiss_index.bin`** (4.1MB) - Domain-specific RAG index binary
- **`index.faiss`** (428MB) - Main FAISS vector index
- **`index.pkl`** (256MB) - Serialized metadata and mappings
- **`metadata.json`** (896KB) - Index configuration and statistics

### Purpose:
These indexes enable fast semantic search across scientific literature for viral immunology research queries. They support the unlimited data retrieval capabilities of the generalized viral analysis workflow.

### Usage:
The MCP server automatically loads these indexes during setup. If missing, run:
```bash
apecx-setup rag
```

### Storage:
Large files (*.faiss, *.bin, *.pkl) are stored using Git LFS (Large File Storage) for efficient version control.

## Related Data Sources:
- VIOLIN database mappings
- BV-BRC genome data
- Scientific literature vectors
- Virus taxonomy information

**Note**: This data directory is essential for the complete viral epitope analysis workflow functionality.
