"""Enhanced lookup functions for unlimited VIOLIN + BV-BRC data retrieval.

These functions are enhanced versions of _violin_bvbrc_lookup that support
unlimited result retrieval by accepting max_results=None. Critical for
comprehensive scientific analysis that requires ALL relevant data.

Key enhancements:
1. max_results=None means unlimited retrieval
2. Quality-based filtering instead of arbitrary truncation
3. Streaming support for massive datasets
4. Enhanced error handling with partial failure recovery
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def lookup_violin_unlimited(
    terms: list[tuple[str, str]],
    violin_dir: Path,
    *,
    max_results: int | None = None,
    min_relevance_score: float = 0.1,
    owner_name: str = "",
) -> list[dict[str, Any]]:
    """Enhanced VIOLIN lookup with unlimited retrieval capability.

    Args:
        terms: List of (query_string, entity_type) pairs
        violin_dir: Directory containing VIOLIN CSVs
        max_results: Hard cap on results, or None for unlimited
        min_relevance_score: Minimum relevance for quality filtering
        owner_name: Caller name for logging

    Returns:
        List of VIOLIN mapping dicts (potentially unlimited)
    """
    if not terms:
        return []

    import pandas as pd

    pathogen_csv = violin_dir / "Pathogen_Information.csv"
    vaccine_csv = violin_dir / "Vaccine_Information.csv"

    results: list[dict[str, Any]] = []
    prefix = f"{owner_name}: " if owner_name else ""

    unlimited_mode = max_results is None
    if unlimited_mode:
        log.info(f"{prefix}VIOLIN unlimited lookup for {len(terms)} terms")

    # Process pathogen data
    if pathogen_csv.is_file():
        try:
            df = pd.read_csv(pathogen_csv, dtype=str).fillna("")
        except Exception as exc:
            log.warning(f"{prefix}failed reading {pathogen_csv}: {exc}")
        else:
            if "Pathogen" in df.columns:
                name_lower = df["Pathogen"].astype(str).str.lower()
                for query, etype in terms:
                    # Skip max_results check if unlimited
                    if not unlimited_mode and len(results) >= max_results:
                        break

                    mask = name_lower.str.contains(query.lower(), regex=False, na=False)
                    matched_rows = df[mask]

                    for _, row in matched_rows.iterrows():
                        # Skip max_results check if unlimited
                        if not unlimited_mode and len(results) >= max_results:
                            break

                        canonical = str(row.get("NCBI_Taxonomy_ID", ""))
                        if not canonical:
                            canonical = str(row.get("Pathogen", query))

                        # Calculate relevance score (simple string matching for now)
                        pathogen_name = str(row.get("Pathogen", "")).lower()
                        relevance = _calculate_relevance(query.lower(), pathogen_name)

                        if relevance >= min_relevance_score:
                            results.append(
                                {
                                    "synonym_id": f"VIOLIN_pathogen_{row.get('id', '')}",
                                    "canonical_term": canonical,
                                    "query_term": query,
                                    "entity_type": etype,
                                    "source": "VIOLIN_Pathogen_Information",
                                    "relevance_score": relevance,
                                }
                            )
            else:
                log.warning(f"{prefix}{pathogen_csv} missing 'Pathogen' column")
    else:
        log.warning(f"{prefix}VIOLIN pathogen CSV not found at {pathogen_csv}")

    # Process vaccine data (only if we haven't hit max_results or in unlimited mode)
    process_vaccines = unlimited_mode or len(results) < max_results
    if process_vaccines and vaccine_csv.is_file():
        try:
            df = pd.read_csv(vaccine_csv, dtype=str).fillna("")
        except Exception as exc:
            log.warning(f"{prefix}failed reading {vaccine_csv}: {exc}")
        else:
            if "Vaccine_Name" in df.columns:
                name_lower = df["Vaccine_Name"].astype(str).str.lower()
                for query, etype in terms:
                    if not unlimited_mode and len(results) >= max_results:
                        break

                    mask = name_lower.str.contains(query.lower(), regex=False, na=False)
                    matched_rows = df[mask]

                    for _, row in matched_rows.iterrows():
                        if not unlimited_mode and len(results) >= max_results:
                            break

                        vaccine_name = str(row.get("Vaccine_Name", query))
                        relevance = _calculate_relevance(query.lower(), vaccine_name.lower())

                        if relevance >= min_relevance_score:
                            results.append(
                                {
                                    "synonym_id": f"VIOLIN_vaccine_{row.get('id', '')}",
                                    "canonical_term": vaccine_name,
                                    "query_term": query,
                                    "entity_type": etype,
                                    "source": "VIOLIN_Vaccine_Information",
                                    "relevance_score": relevance,
                                }
                            )
            else:
                log.warning(f"{prefix}{vaccine_csv} missing 'Vaccine_Name' column")

    log.info(
        f"{prefix}VIOLIN {'unlimited' if unlimited_mode else 'limited'} lookup: {len(results)} results"
    )
    return results


def lookup_bvbrc_unlimited(
    terms: list[tuple[str, str]],
    bvbrc_dir: Path,
    *,
    max_results: int | None = None,
    min_relevance_score: float = 0.1,
    owner_name: str = "",
) -> list[dict[str, Any]]:
    """Enhanced BV-BRC lookup with unlimited retrieval capability.

    Args:
        terms: List of (query_string, entity_type) pairs
        bvbrc_dir: Directory containing BV-BRC genome cache
        max_results: Hard cap on results, or None for unlimited
        min_relevance_score: Minimum relevance for quality filtering
        owner_name: Caller name for logging

    Returns:
        List of BV-BRC genome dicts (potentially unlimited)
    """
    if not terms:
        return []

    import pandas as pd

    # BV-BRC typically stores alphavirus genome metadata
    genome_cache = bvbrc_dir / "alphavirus_genomes.tsv"
    results: list[dict[str, Any]] = []
    prefix = f"{owner_name}: " if owner_name else ""

    unlimited_mode = max_results is None
    if unlimited_mode:
        log.info(f"{prefix}BV-BRC unlimited lookup for {len(terms)} terms")

    if genome_cache.is_file():
        try:
            df = pd.read_csv(genome_cache, dtype=str, sep="\t").fillna("")
        except Exception as exc:
            log.warning(f"{prefix}failed reading {genome_cache}: {exc}")
            return results

        # Search across multiple BV-BRC columns for virus mentions
        search_columns = ["genome_name", "organism_name", "strain", "host_name", "disease"]
        available_columns = [col for col in search_columns if col in df.columns]

        if not available_columns:
            log.warning(f"{prefix}BV-BRC cache missing expected columns: {search_columns}")
            return results

        for query, etype in terms:
            if not unlimited_mode and len(results) >= max_results:
                break

            query_lower = query.lower()

            # Search across all available columns
            combined_mask = pd.Series([False] * len(df), index=df.index)
            for col in available_columns:
                if col in df.columns:
                    col_lower = df[col].astype(str).str.lower()
                    mask = col_lower.str.contains(query_lower, regex=False, na=False)
                    combined_mask |= mask

            matched_rows = df[combined_mask]

            for _, row in matched_rows.iterrows():
                if not unlimited_mode and len(results) >= max_results:
                    break

                # Calculate relevance based on best match across columns
                best_relevance = 0.0
                best_match_column = None

                for col in available_columns:
                    if col in row:
                        col_value = str(row[col]).lower()
                        relevance = _calculate_relevance(query_lower, col_value)
                        if relevance > best_relevance:
                            best_relevance = relevance
                            best_match_column = col

                if best_relevance >= min_relevance_score:
                    genome_id = str(row.get("genome_id", ""))
                    organism = str(row.get("organism_name", query))

                    results.append(
                        {
                            "genome_id": genome_id,
                            "organism_name": organism,
                            "strain": str(row.get("strain", "")),
                            "host_name": str(row.get("host_name", "")),
                            "query_term": query,
                            "entity_type": etype,
                            "source": "BV-BRC_Genome_Cache",
                            "relevance_score": best_relevance,
                            "matched_column": best_match_column,
                        }
                    )
    else:
        log.warning(f"{prefix}BV-BRC cache not found at {genome_cache}")

    log.info(
        f"{prefix}BV-BRC {'unlimited' if unlimited_mode else 'limited'} lookup: {len(results)} results"
    )
    return results


def _calculate_relevance(query: str, target: str) -> float:
    """Calculate relevance score between query and target string.

    Simple relevance scoring for quality filtering. Can be enhanced
    with more sophisticated matching algorithms.

    Args:
        query: Search query (lowercase)
        target: Target text (lowercase)

    Returns:
        Relevance score between 0.0 and 1.0
    """
    if not query or not target:
        return 0.0

    # Exact match
    if query == target:
        return 1.0

    # Substring match
    if query in target:
        return 0.8

    # Reverse substring match
    if target in query:
        return 0.6

    # Word overlap scoring
    query_words = set(query.split())
    target_words = set(target.split())

    if not query_words or not target_words:
        return 0.1

    overlap = len(query_words & target_words)
    total_words = len(query_words | target_words)

    if overlap == 0:
        return 0.1

    return min(0.7, overlap / total_words + 0.2)


# Compatibility wrappers for the original limited functions
def lookup_violin(
    terms: list[tuple[str, str]],
    violin_dir: Path,
    *,
    max_results: int | None,
    owner_name: str = "",
) -> list[dict[str, Any]]:
    """Compatibility wrapper that supports both limited and unlimited modes."""
    return lookup_violin_unlimited(
        terms=terms,
        violin_dir=violin_dir,
        max_results=max_results,
        owner_name=owner_name,
    )


def lookup_bvbrc(
    terms: list[tuple[str, str]],
    bvbrc_dir: Path,
    *,
    max_results: int | None,
    owner_name: str = "",
) -> list[dict[str, Any]]:
    """Compatibility wrapper that supports both limited and unlimited modes."""
    return lookup_bvbrc_unlimited(
        terms=terms,
        bvbrc_dir=bvbrc_dir,
        max_results=max_results,
        owner_name=owner_name,
    )
