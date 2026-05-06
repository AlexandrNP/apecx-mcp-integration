"""Stateless lookup functions for VIOLIN + BV-BRC tabular data.

Extracted from ``VIOLINBVBRCContextStep`` so that
``SynthesisContextAssemblyStep`` (the synthesis-pipeline fan-in step)
can call the same logic WITHOUT having to construct a step instance via
``object.__new__`` — the prior pattern was a documented corner cut that
violated the nanobrain rule that step instances must always be created
through ``from_config``.

These functions are pure: their output depends only on their arguments.
The ``owner_name`` parameter only flows into log lines so an operator
can correlate WARNINGs back to the calling step.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def lookup_violin(
    terms: list[tuple[str, str]],
    violin_dir: Path,
    *,
    max_results: int,
    owner_name: str = "",
) -> list[dict[str, Any]]:
    """Substring-match ``terms`` against VIOLIN's pathogen + vaccine CSVs.

    Args:
        terms: List of ``(query_string, entity_type)`` pairs. Empty
            list short-circuits to ``[]``.
        violin_dir: Directory containing ``Pathogen_Information.csv``
            and ``Vaccine_Information.csv``.
        max_results: Hard cap on returned mappings.
        owner_name: Caller's step name; only used in log messages.

    Returns:
        List of mapping dicts with keys ``synonym_id``,
        ``canonical_term``, ``query_term``, ``entity_type``, ``source``.
    """
    if not terms:
        return []

    # Local import — pandas is heavy and not all callers of this module
    # need it eagerly imported.
    import pandas as pd

    pathogen_csv = violin_dir / "Pathogen_Information.csv"
    vaccine_csv = violin_dir / "Vaccine_Information.csv"

    results: list[dict[str, Any]] = []
    prefix = f"{owner_name}: " if owner_name else ""

    if pathogen_csv.is_file():
        try:
            df = pd.read_csv(pathogen_csv, dtype=str).fillna("")
        except Exception as exc:
            log.warning("%sfailed reading %s: %s", prefix, pathogen_csv, exc)
        else:
            if "Pathogen" in df.columns:
                name_lower = df["Pathogen"].astype(str).str.lower()
                for query, etype in terms:
                    if len(results) >= max_results:
                        break
                    mask = name_lower.str.contains(query.lower(), regex=False, na=False)
                    for _, row in df[mask].iterrows():
                        if len(results) >= max_results:
                            break
                        canonical = str(row.get("NCBI_Taxonomy_ID", ""))
                        if not canonical:
                            canonical = str(row.get("Pathogen", query))
                        results.append(
                            {
                                "synonym_id": f"VIOLIN_pathogen_{row.get('id', '')}",
                                "canonical_term": canonical,
                                "query_term": query,
                                "entity_type": etype,
                                "source": "VIOLIN_Pathogen_Information",
                            }
                        )
            else:
                log.warning(
                    "%s%s missing 'Pathogen' column",
                    prefix,
                    pathogen_csv,
                )
    else:
        log.warning(
            "%sVIOLIN pathogen CSV not found at %s — returning no pathogen mappings",
            prefix,
            pathogen_csv,
        )

    if len(results) < max_results and vaccine_csv.is_file():
        try:
            df = pd.read_csv(vaccine_csv, dtype=str).fillna("")
        except Exception as exc:
            log.warning("%sfailed reading %s: %s", prefix, vaccine_csv, exc)
        else:
            # Vaccine CSV uses ``Vaccine_Name`` for the display name and
            # ``Vaccine_Ontology_ID`` for the canonical term. Some rows
            # have only ``Vaccine``; match both.
            name_cols = [c for c in ("Vaccine_Name", "Vaccine") if c in df.columns]
            if name_cols:
                for query, etype in terms:
                    if len(results) >= max_results:
                        break
                    mask = None
                    for col in name_cols:
                        col_lower = df[col].astype(str).str.lower()
                        sub = col_lower.str.contains(query.lower(), regex=False, na=False)
                        mask = sub if mask is None else (mask | sub)
                    if mask is None:
                        continue
                    for _, row in df[mask].iterrows():
                        if len(results) >= max_results:
                            break
                        vac_canonical = str(row.get("Vaccine_Ontology_ID", ""))
                        if not vac_canonical:
                            vac_canonical = str(
                                row.get("Vaccine_Name", "") or row.get("Vaccine", query)
                            )
                        results.append(
                            {
                                "synonym_id": f"VIOLIN_vaccine_{row.get('id', '')}",
                                "canonical_term": vac_canonical,
                                "query_term": query,
                                # Vaccine matches override entity_type to
                                # ``vaccine`` — the upstream entity type
                                # may say ``pathogen`` but we matched on
                                # a vaccine row.
                                "entity_type": "vaccine" if etype == "unknown" else etype,
                                "source": "VIOLIN_Vaccine_Information",
                            }
                        )
            else:
                log.warning(
                    "%s%s missing vaccine name columns",
                    prefix,
                    vaccine_csv,
                )
    elif not vaccine_csv.is_file():
        log.warning(
            "%sVIOLIN vaccine CSV not found at %s — returning no vaccine mappings",
            prefix,
            vaccine_csv,
        )

    return results


def lookup_bvbrc(
    terms: list[tuple[str, str]],
    bvbrc_dir: Path,
    *,
    max_results: int,
    owner_name: str = "",
) -> list[dict[str, Any]]:
    """Substring-match ``terms`` against the BV-BRC alphavirus genomes TSV.

    Args:
        terms: List of ``(query_string, entity_type)`` pairs. The
            entity type is unused but kept for shape symmetry with
            ``lookup_violin``.
        bvbrc_dir: Directory containing ``alphavirus_genomes.tsv``.
        max_results: Hard cap on returned genomes.
        owner_name: Caller's step name; only used in log messages.

    Returns:
        List of dicts with keys ``genome_id`` and ``genome_name``.
    """
    if not terms:
        return []

    import pandas as pd

    prefix = f"{owner_name}: " if owner_name else ""
    tsv_path = bvbrc_dir / "alphavirus_genomes.tsv"
    if not tsv_path.is_file():
        log.warning(
            "%sBV-BRC TSV not found at %s — returning no genomes",
            prefix,
            tsv_path,
        )
        return []

    try:
        df = pd.read_csv(tsv_path, sep="\t", dtype=str).fillna("")
    except Exception as exc:
        log.warning("%sfailed reading %s: %s", prefix, tsv_path, exc)
        return []

    if "genome.genome_name" not in df.columns:
        log.warning(
            "%s%s missing 'genome.genome_name' column",
            prefix,
            tsv_path,
        )
        return []

    name_lower = df["genome.genome_name"].astype(str).str.lower()
    results: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for query, _etype in terms:
        if len(results) >= max_results:
            break
        mask = name_lower.str.contains(query.lower(), regex=False, na=False)
        for _, row in df[mask].iterrows():
            if len(results) >= max_results:
                break
            gid = str(row.get("genome.genome_id", ""))
            if gid in seen_ids:
                continue
            seen_ids.add(gid)
            results.append(
                {
                    "genome_id": gid,
                    "genome_name": str(row.get("genome.genome_name", "")),
                }
            )
    return results


__all__ = ["lookup_bvbrc", "lookup_violin"]
