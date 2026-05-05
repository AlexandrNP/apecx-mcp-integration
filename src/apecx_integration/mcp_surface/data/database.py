"""VIOLIN and BV-BRC database query layer.

Vendored from ``apecx-mcp/src/apecx_mcp/database.py``
(2026-04-27, B-1 choice in the integration MCP rollout). See the
package ``__init__`` docstring for drift policy.

Pure pandas, read-only. No LLM, no nanobrain, no Control Plane —
the integration MCP can call these tools whether the Control Plane
is reachable or not. ``DatabaseStore`` is loaded lazily on first
tool call (``get_store``) so the MCP transport's startup latency is
unaffected when no DB tools are invoked.

Data layout (rooted at ``$APECX_DATA_ROOT`` / ``$APECX_ROOT/data``):

    violin/Vaccine_Information.csv
    violin/Pathogen_Information.csv
    violin/Gene_Information.csv
    violin/Vaccine_Pathogen_Information.csv
    violin/Gene_Vaccine_Pathogen_Information.csv
    BVBRC_genome_alphavirus.csv
    bvbrc_cache/*.tsv          (optional)
    virus_resolution_cache/*.json (optional)

If a CSV is missing, the corresponding tool returns
``{"error": "..."}`` instead of raising. This keeps the MCP server
useful in partial-data deploys.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


_VIOLIN_CSV_FILES: dict[str, str] = {
    "vaccines": "Vaccine_Information.csv",
    "pathogens": "Pathogen_Information.csv",
    "genes": "Gene_Information.csv",
    "gene_vaccine_pathogen": "Gene_Vaccine_Pathogen_Information.csv",
    "vaccine_pathogen": "Vaccine_Pathogen_Information.csv",
}


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class DatabaseStore:
    """Holds VIOLIN + BV-BRC DataFrames in memory. Loaded once, read-only."""

    def __init__(
        self,
        violin_csv_paths: dict[str, Path],
        bvbrc_csv_path: Path | None,
        bvbrc_cache_dir: Path | None,
        virus_resolution_cache_dir: Path | None,
    ) -> None:
        self.dfs: dict[str, pd.DataFrame] = {}
        self.virus_cache: dict[str, dict] = {}

        for key, path in violin_csv_paths.items():
            try:
                self.dfs[key] = pd.read_csv(path, low_memory=False)
                logger.info("Loaded %s: %d rows", key, len(self.dfs[key]))
            except Exception as e:
                logger.error("Failed to load %s from %s: %s", key, path, e)

        if bvbrc_csv_path:
            try:
                self.dfs["bvbrc_genomes"] = pd.read_csv(bvbrc_csv_path, low_memory=False)
                logger.info("Loaded bvbrc_genomes: %d rows", len(self.dfs["bvbrc_genomes"]))
            except Exception as e:
                logger.error("Failed to load BV-BRC genomes: %s", e)

        if bvbrc_cache_dir and bvbrc_cache_dir.is_dir():
            for tsv_path in sorted(bvbrc_cache_dir.glob("*.tsv")):
                key = f"bvbrc_cache_{tsv_path.stem}"
                try:
                    self.dfs[key] = pd.read_csv(tsv_path, sep="\t", low_memory=False)
                    logger.info("Loaded %s: %d rows", key, len(self.dfs[key]))
                except Exception as e:
                    logger.error("Failed to load %s: %s", tsv_path, e)

        if virus_resolution_cache_dir and virus_resolution_cache_dir.is_dir():
            for json_path in sorted(virus_resolution_cache_dir.glob("*.json")):
                try:
                    data = json.loads(json_path.read_text())
                    name = data.get("canonical_name", json_path.stem)
                    self.virus_cache[name.lower()] = data
                except Exception as e:
                    logger.error("Failed to load virus cache %s: %s", json_path, e)

    def is_loaded(self) -> bool:
        return len(self.dfs) > 0


# ---------------------------------------------------------------------------
# Lazy environment-driven loader
# ---------------------------------------------------------------------------


_store_singleton: DatabaseStore | None = None
_store_load_error: str | None = None


def _resolve_data_root() -> Path | None:
    """Return the data dir from APECX_DATA_ROOT, or APECX_ROOT/data, else None."""
    explicit = os.environ.get("APECX_DATA_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()
    workspace = os.environ.get("APECX_ROOT")
    if workspace:
        return (Path(workspace).expanduser() / "data").resolve()
    return None


def get_store() -> tuple[DatabaseStore | None, str | None]:
    """Lazily load the store from env. Returns ``(store, error_msg)``.

    On first call: probes the data dir, instantiates DatabaseStore,
    caches it process-wide. On miss (env vars unset), returns
    ``(None, "<reason>")`` — callers surface this back through MCP
    rather than raising, so a missing data dir doesn't break tool
    invocation infrastructure.

    Subsequent calls return the cached singleton in O(1).
    """
    global _store_singleton, _store_load_error
    if _store_singleton is not None:
        return _store_singleton, None
    if _store_load_error is not None:
        # Don't keep retrying the same failure on every tool call.
        return None, _store_load_error

    data_root = _resolve_data_root()
    if data_root is None:
        _store_load_error = (
            "No data directory configured. Set APECX_DATA_ROOT (preferred) "
            "or APECX_ROOT (the workspace root containing data/) before "
            "calling the database tools."
        )
        return None, _store_load_error
    if not data_root.is_dir():
        _store_load_error = (
            f"Configured data root {data_root} does not exist or is not a directory."
        )
        return None, _store_load_error

    violin_dir = data_root / "violin"
    violin_paths: dict[str, Path] = {}
    for key, fname in _VIOLIN_CSV_FILES.items():
        path = violin_dir / fname
        if path.exists():
            violin_paths[key] = path
        else:
            logger.warning("VIOLIN CSV not found: %s", path)

    bvbrc_csv = data_root / "BVBRC_genome_alphavirus.csv"
    bvbrc_path: Path | None = bvbrc_csv if bvbrc_csv.exists() else None

    bvbrc_cache = data_root / "bvbrc_cache"
    bvbrc_cache_dir: Path | None = bvbrc_cache if bvbrc_cache.is_dir() else None

    vrc = data_root / "virus_resolution_cache"
    vrc_dir: Path | None = vrc if vrc.is_dir() else None

    if not violin_paths and bvbrc_path is None:
        _store_load_error = (
            f"No VIOLIN or BV-BRC CSVs found under {data_root}. Expected "
            f"violin/*.csv and/or BVBRC_genome_alphavirus.csv."
        )
        return None, _store_load_error

    store = DatabaseStore(
        violin_csv_paths=violin_paths,
        bvbrc_csv_path=bvbrc_path,
        bvbrc_cache_dir=bvbrc_cache_dir,
        virus_resolution_cache_dir=vrc_dir,
    )
    _store_singleton = store
    return store, None


def reset_store() -> None:
    """Test hook — clear the cached singleton + error.

    Production code never calls this. Tests use it to swap in a
    fixture-loaded DatabaseStore between cases.
    """
    global _store_singleton, _store_load_error
    _store_singleton = None
    _store_load_error = None


def set_store_for_tests(store: DatabaseStore | None) -> None:
    """Test hook — install a pre-built store as the singleton.

    Only intended for tests that load a fixture CSV under tmp_path
    and want subsequent ``get_store()`` calls to see it.
    """
    global _store_singleton, _store_load_error
    _store_singleton = store
    _store_load_error = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_str_contains(df: pd.DataFrame, columns: list[str], term: str) -> pd.DataFrame:
    """Case-insensitive substring search across multiple columns. NaN-safe."""
    mask = pd.Series(False, index=df.index)
    term_lower = term.lower()
    for col in columns:
        if col in df.columns:
            mask |= df[col].astype(str).str.lower().str.contains(term_lower, na=False, regex=False)
    return df[mask]


def _df_to_records(df: pd.DataFrame, limit: int) -> list[dict[str, Any]]:
    """Convert DataFrame slice to list of dicts, replacing NaN with None."""
    return df.head(limit).where(df.notna(), None).to_dict(orient="records")


# ---------------------------------------------------------------------------
# Query functions
# ---------------------------------------------------------------------------


def query_vaccines(
    store: DatabaseStore,
    search_term: str | None = None,
    vaccine_type: str | None = None,
    status: str | None = None,
    pathogen: str | None = None,
    limit: int = 25,
    vo_id: str | None = None,
) -> dict[str, Any]:
    """``vo_id``: bare VO id string (e.g. ``"VO_0000122"``) for precision
    filtering.  When supplied, replaces substring search on search_term."""
    if "vaccines" not in store.dfs:
        return {"error": "Vaccine data not loaded"}

    df = store.dfs["vaccines"].copy()

    if vo_id is not None and "Vaccine_Ontology_ID" in df.columns:
        df = df[df["Vaccine_Ontology_ID"].astype(str).str.strip() == vo_id]
    elif search_term:
        df = _safe_str_contains(
            df,
            ["Vaccine", "Vaccine_Name", "Type", "Antigen", "Description", "Tradename"],
            search_term,
        )

    if vaccine_type:
        df = _safe_str_contains(df, ["Type"], vaccine_type)

    if status:
        df = _safe_str_contains(df, ["Status"], status)

    if pathogen and "vaccine_pathogen" in store.dfs and "pathogens" in store.dfs:
        p_df = _safe_str_contains(store.dfs["pathogens"], ["Pathogen", "Disease"], pathogen)
        if not p_df.empty:
            vp_df = store.dfs["vaccine_pathogen"]
            matched_pathogen_ids = p_df["id"].tolist()
            matched_vaccine_ids = vp_df[vp_df["pathogen_id"].isin(matched_pathogen_ids)][
                "vaccine_id"
            ].tolist()
            df = df[df["id"].isin(matched_vaccine_ids)]

    return {
        "vaccines": _df_to_records(df, limit),
        "count": min(len(df), limit),
        "total_matching": len(df),
        "total_in_database": len(store.dfs["vaccines"]),
    }


def query_pathogens(
    store: DatabaseStore,
    search_term: str | None = None,
    disease: str | None = None,
    limit: int = 25,
    ncbi_taxonomy_id: int | None = None,
    ncbi_taxonomy_ids: list[int] | None = None,
) -> dict[str, Any]:
    """``ncbi_taxonomy_id``: single taxon ID for leaf-level precision.
    ``ncbi_taxonomy_ids``: set of taxon IDs for hierarchy-expanded queries.
    When either is supplied, replaces the substring search on search_term.
    ``ncbi_taxonomy_ids`` takes priority over ``ncbi_taxonomy_id``."""
    if "pathogens" not in store.dfs:
        return {"error": "Pathogen data not loaded"}

    df = store.dfs["pathogens"].copy()

    if ncbi_taxonomy_ids is not None and "NCBI_Taxonomy_ID" in df.columns:
        id_set = set(ncbi_taxonomy_ids)
        df = df[pd.to_numeric(df["NCBI_Taxonomy_ID"], errors="coerce").isin(id_set)]
    elif ncbi_taxonomy_id is not None and "NCBI_Taxonomy_ID" in df.columns:
        df = df[pd.to_numeric(df["NCBI_Taxonomy_ID"], errors="coerce") == ncbi_taxonomy_id]
    elif search_term:
        df = _safe_str_contains(df, ["Pathogen", "Disease", "Pathogen_Description"], search_term)

    if disease:
        df = _safe_str_contains(df, ["Disease"], disease)

    records = _df_to_records(df, limit)
    if "vaccine_pathogen" in store.dfs:
        vp_df = store.dfs["vaccine_pathogen"]
        for rec in records:
            pid = rec.get("id")
            if pid is not None:
                rec["vaccine_count"] = int(vp_df[vp_df["pathogen_id"] == pid].shape[0])

    return {
        "pathogens": records,
        "count": min(len(df), limit),
        "total_matching": len(df),
        "total_in_database": len(store.dfs["pathogens"]),
    }


def query_genes(
    store: DatabaseStore,
    search_term: str | None = None,
    organism: str | None = None,
    limit: int = 25,
    ncbi_gene_id: int | None = None,
) -> dict[str, Any]:
    """``ncbi_gene_id``: integer gene ID for precision filtering.
    When supplied, replaces the substring search on search_term."""
    if "genes" not in store.dfs:
        return {"error": "Gene data not loaded"}

    df = store.dfs["genes"].copy()

    if ncbi_gene_id is not None and "NCBI_Gene_ID" in df.columns:
        df = df[pd.to_numeric(df["NCBI_Gene_ID"], errors="coerce") == ncbi_gene_id]
    elif search_term:
        df = _safe_str_contains(
            df, ["Gene_Name", "Protein_Name", "Molecule_Role", "Organism"], search_term
        )

    if organism:
        df = _safe_str_contains(df, ["Organism"], organism)

    return {
        "genes": _df_to_records(df, limit),
        "count": min(len(df), limit),
        "total_matching": len(df),
        "total_in_database": len(store.dfs["genes"]),
    }


def query_bvbrc_genomes(
    store: DatabaseStore,
    search_term: str | None = None,
    species: str | None = None,
    host: str | None = None,
    country: str | None = None,
    min_year: int | None = None,
    max_year: int | None = None,
    limit: int = 25,
    ncbi_taxonomy_id: int | None = None,
) -> dict[str, Any]:
    """``ncbi_taxonomy_id``: integer taxon ID for precision filtering on the
    ``NCBI Taxon ID`` column.  When supplied, replaces substring search on
    search_term."""
    if "bvbrc_genomes" not in store.dfs:
        return {"error": "BV-BRC genome data not loaded"}

    df = store.dfs["bvbrc_genomes"].copy()

    if ncbi_taxonomy_id is not None and "NCBI Taxon ID" in df.columns:
        df = df[pd.to_numeric(df["NCBI Taxon ID"], errors="coerce") == ncbi_taxonomy_id]
    elif search_term:
        df = _safe_str_contains(
            df,
            [
                "Genome Name",
                "Species",
                "Strain",
                "Host Name",
                "Host Common Name",
                "Other Names",
            ],
            search_term,
        )

    if species:
        df = _safe_str_contains(df, ["Species"], species)

    if host:
        df = _safe_str_contains(df, ["Host Name", "Host Common Name"], host)

    if country:
        df = _safe_str_contains(df, ["Isolation Country", "Geographic Location"], country)

    if min_year is not None and "Collection Year" in df.columns:
        df = df[pd.to_numeric(df["Collection Year"], errors="coerce") >= min_year]

    if max_year is not None and "Collection Year" in df.columns:
        df = df[pd.to_numeric(df["Collection Year"], errors="coerce") <= max_year]

    return {
        "genomes": _df_to_records(df, limit),
        "count": min(len(df), limit),
        "total_matching": len(df),
        "total_in_database": len(store.dfs["bvbrc_genomes"]),
    }


def get_vaccine_pathogen_genes(
    store: DatabaseStore,
    pathogen_name: str,
    ncbi_taxonomy_id: int | None = None,
) -> dict[str, Any]:
    """``ncbi_taxonomy_id``: integer taxon ID for precision pathogen matching.
    When supplied, replaces substring search on pathogen_name."""
    required = {"pathogens", "vaccines", "vaccine_pathogen", "gene_vaccine_pathogen", "genes"}
    missing = required - set(store.dfs.keys())
    if missing:
        return {"error": f"Missing tables: {', '.join(sorted(missing))}"}

    if ncbi_taxonomy_id is not None and "NCBI_Taxonomy_ID" in store.dfs["pathogens"].columns:
        pathogens_df = store.dfs["pathogens"]
        pathogens_df = pathogens_df[
            pd.to_numeric(pathogens_df["NCBI_Taxonomy_ID"], errors="coerce") == ncbi_taxonomy_id
        ]
    else:
        pathogens_df = _safe_str_contains(
            store.dfs["pathogens"], ["Pathogen", "Disease"], pathogen_name
        )
    if pathogens_df.empty:
        return {"pathogen": pathogen_name, "vaccines": [], "total_vaccines": 0, "total_genes": 0}

    vp_df = store.dfs["vaccine_pathogen"]
    gvp_df = store.dfs["gene_vaccine_pathogen"]
    vaccines_df = store.dfs["vaccines"]
    genes_df = store.dfs["genes"]

    pathogen_ids = pathogens_df["id"].tolist()
    vp_matches = vp_df[vp_df["pathogen_id"].isin(pathogen_ids)]
    vaccine_ids = vp_matches["vaccine_id"].unique().tolist()

    results = []
    total_genes = 0
    for vid in vaccine_ids:
        vax_row = vaccines_df[vaccines_df["id"] == vid]
        if vax_row.empty:
            continue
        vax = vax_row.iloc[0]

        vp_ids = vp_matches[vp_matches["vaccine_id"] == vid]["id"].tolist()
        gene_links = gvp_df[gvp_df["vaccine_pathogen_id"].isin(vp_ids)]
        gene_ids = gene_links["gene_id"].unique().tolist()
        gene_rows = genes_df[genes_df["id"].isin(gene_ids)]

        gene_list = []
        for _, g in gene_rows.iterrows():
            gene_list.append(
                {
                    "gene_name": g.get("Gene_Name"),
                    "protein_name": g.get("Protein_Name"),
                    "organism": g.get("Organism"),
                    "molecule_role": g.get("Molecule_Role"),
                }
            )
        total_genes += len(gene_list)

        results.append(
            {
                "vaccine_name": vax.get("Vaccine_Name") or vax.get("Vaccine"),
                "type": vax.get("Type"),
                "status": vax.get("Status"),
                "genes": gene_list,
            }
        )

    pathogen_display = pathogens_df.iloc[0]["Pathogen"] if not pathogens_df.empty else pathogen_name

    return {
        "pathogen": pathogen_display,
        "vaccines": results,
        "total_vaccines": len(results),
        "total_genes": total_genes,
    }


def database_statistics(store: DatabaseStore) -> dict[str, Any]:
    tables = {}
    for key, df in store.dfs.items():
        tables[key] = {
            "rows": len(df),
            "columns": list(df.columns),
        }
    return {"tables": tables, "virus_resolution_cache_entries": len(store.virus_cache)}


def resolve_entity(store: DatabaseStore, name: str) -> dict[str, Any]:
    """Resolve a biomedical entity name across all loaded databases."""
    matches: dict[str, Any] = {
        "pathogens": [],
        "vaccines": [],
        "genes": [],
        "bvbrc_genomes": [],
    }
    identifiers: dict[str, list] = {
        "ncbi_taxonomy_ids": [],
        "violin_pathogen_ids": [],
        "violin_vaccine_ids": [],
    }

    cached = store.virus_cache.get(name.lower())

    if "pathogens" in store.dfs:
        p_df = _safe_str_contains(store.dfs["pathogens"], ["Pathogen", "Disease"], name)
        for _, row in p_df.iterrows():
            matches["pathogens"].append(
                {
                    "id": int(row["id"]) if pd.notna(row.get("id")) else None,
                    "name": row.get("Pathogen"),
                    "ncbi_taxonomy_id": row.get("NCBI_Taxonomy_ID"),
                    "violin_id": row.get("VIOLIN_c_pathogen_id"),
                    "disease": row.get("Disease"),
                }
            )
            if pd.notna(row.get("NCBI_Taxonomy_ID")):
                tid = str(row["NCBI_Taxonomy_ID"])
                if tid not in identifiers["ncbi_taxonomy_ids"]:
                    identifiers["ncbi_taxonomy_ids"].append(tid)
            if pd.notna(row.get("VIOLIN_c_pathogen_id")):
                vid = str(row["VIOLIN_c_pathogen_id"])
                if vid not in identifiers["violin_pathogen_ids"]:
                    identifiers["violin_pathogen_ids"].append(vid)

    if "vaccines" in store.dfs:
        v_df = _safe_str_contains(
            store.dfs["vaccines"],
            ["Vaccine", "Vaccine_Name", "Tradename", "Antigen"],
            name,
        )
        for _, row in v_df.head(10).iterrows():
            matches["vaccines"].append(
                {
                    "id": int(row["id"]) if pd.notna(row.get("id")) else None,
                    "name": row.get("Vaccine_Name") or row.get("Vaccine"),
                    "type": row.get("Type"),
                    "status": row.get("Status"),
                }
            )

    if "genes" in store.dfs:
        g_df = _safe_str_contains(
            store.dfs["genes"], ["Gene_Name", "Protein_Name", "Organism"], name
        )
        for _, row in g_df.head(10).iterrows():
            matches["genes"].append(
                {
                    "id": int(row["id"]) if pd.notna(row.get("id")) else None,
                    "name": row.get("Gene_Name"),
                    "protein_name": row.get("Protein_Name"),
                    "organism": row.get("Organism"),
                }
            )

    if "bvbrc_genomes" in store.dfs:
        bg_df = _safe_str_contains(
            store.dfs["bvbrc_genomes"],
            ["Genome Name", "Species", "Other Names", "Strain"],
            name,
        )
        if not bg_df.empty:
            species_col = "Species" if "Species" in bg_df.columns else None
            if species_col:
                for sp, group in bg_df.groupby(species_col):
                    matches["bvbrc_genomes"].append(
                        {
                            "species": sp,
                            "count": len(group),
                            "example_genome_id": group.iloc[0].get("Genome ID"),
                        }
                    )
            else:
                matches["bvbrc_genomes"].append(
                    {
                        "species": "unknown",
                        "count": len(bg_df),
                    }
                )

    return {
        "query": name,
        "matches": matches,
        "identifiers": identifiers,
        "virus_resolution_cache": cached,
    }


__all__ = [
    "DatabaseStore",
    "database_statistics",
    "get_store",
    "get_vaccine_pathogen_genes",
    "query_bvbrc_genomes",
    "query_genes",
    "query_pathogens",
    "query_vaccines",
    "reset_store",
    "resolve_entity",
    "set_store_for_tests",
]
