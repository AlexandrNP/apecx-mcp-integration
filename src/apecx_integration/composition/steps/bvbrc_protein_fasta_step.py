"""BvbrcProteinFastaStep — fetch REAL per-strain protein AA sequences from BV-BRC (EO-51).

The conserved-sites workflow needs actual amino-acid sequences for a virus's protein across
many strains. Those sequences are NOT in the harmonized Globus metadata indices (which carry
identifiers/labels/counts only), so this step queries the live BV-BRC data API:

  1. ``genome_feature`` — features for the taxon matching the target protein, returning
     ``patric_id`` / ``product`` / ``genome_name`` / ``aa_sequence_md5`` (one row per strain).
  2. ``feature_sequence`` — the actual AA ``sequence`` keyed by ``md5`` (batched).

NO mocks, NO placeholder sequences, NO silent degradation: a network error or an empty result
raises (FAIL-LOUD). This step deliberately replaces the abandoned ``SequenceAnalysisStep``,
which wrote ``"ATCGATCG"*20`` placeholders and copied input as a "mock alignment" — a
fake-data pipeline that produced plausible-looking but meaningless conservation output.
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from nanobrain.core.step import BaseStep, StepConfig
from pydantic import Field

log = logging.getLogger(__name__)

# feature_sequence batched lookups — keep each request URL well under server limits.
_MD5_BATCH = 40


class BvbrcProteinFastaStepConfig(StepConfig):
    """Config for BV-BRC protein-sequence retrieval.

    Inherits StepConfig's ``extra='allow'`` (the framework injects metadata fields like
    ``source_path`` at load time) — a StepConfig subclass must NOT set ``extra='forbid'``,
    unlike a plain data model.
    """

    bvbrc_api_base: str = Field(default="https://www.bv-brc.org/api")
    feature_type: str = Field(
        default="CDS",
        description="BV-BRC feature_type to pull (e.g. 'CDS' for whole CDS, 'mat_peptide' "
        "for mature peptides). The per-call input may override this.",
    )
    max_sequences: int = Field(default=50, ge=2)
    request_timeout_seconds: float = Field(default=60.0, gt=0)
    length_cluster_tolerance: float = Field(
        default=0.2,
        gt=0.0,
        le=1.0,
        description="Length-cluster selection: keep the DOMINANT (most-populous) length band and "
        "drop outliers, so a few mis-annotated/partial records (e.g. a 1180aa polyprotein among "
        "~495aa envelope E proteins, or short genome fragments) do NOT define the keep threshold. "
        "A record is in the dominant band when its length is within ±this fraction of the modal "
        "central length (the length whose ±tolerance window contains the most sequences). 0.2 keeps "
        "a coherent set that MAFFT can align without gap-blurring while tolerating real "
        "indel-length variation. This REPLACES the former 'fraction of the single longest' filter, "
        "which a single long outlier could collapse to <2 sequences.",
    )


class BvbrcProteinFastaStep(BaseStep):
    """Fetch real per-strain protein AA sequences for a taxon from the BV-BRC data API."""

    COMPONENT_TYPE = "bvbrc_protein_fasta_step"

    @classmethod
    def _get_config_class(cls):
        return BvbrcProteinFastaStepConfig

    def _init_from_config(self, config, component_config, dependencies) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._api_base: str = getattr(config, "bvbrc_api_base", "https://www.bv-brc.org/api")
        self._feature_type: str = getattr(config, "feature_type", "CDS")
        self._max_sequences: int = int(getattr(config, "max_sequences", 50))
        self._timeout: float = float(getattr(config, "request_timeout_seconds", 60.0))
        self._length_cluster_tolerance: float = float(
            getattr(config, "length_cluster_tolerance", 0.2)
        )

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        import asyncio

        payload = self._unwrap(input_data)
        taxon_id = payload.get("taxon_id")
        protein = payload.get("protein")
        feature_type = payload.get("feature_type") or self._feature_type

        if not isinstance(taxon_id, int) and not (isinstance(taxon_id, str) and taxon_id.isdigit()):
            raise ValueError(
                f"BvbrcProteinFastaStep '{self.name}': 'taxon_id' must be an NCBI taxon id "
                f"(int or digit string); got {taxon_id!r}"
            )
        taxon_id = int(taxon_id)
        if not isinstance(protein, str) or not protein.strip():
            raise ValueError(
                f"BvbrcProteinFastaStep '{self.name}': 'protein' must be a non-empty product "
                f"name/substring (e.g. 'E1', 'capsid'); got {protein!r}"
            )
        protein = protein.strip()

        records = await asyncio.to_thread(self._fetch, taxon_id, protein, feature_type)

        if not records:
            raise ValueError(
                f"BvbrcProteinFastaStep '{self.name}': BV-BRC returned no {protein!r} "
                f"({feature_type}) protein features for taxon {taxon_id}. Check the protein "
                f"product name and feature_type, or that BV-BRC has coverage for this taxon."
            )
        if len(records) < 2:
            raise ValueError(
                f"BvbrcProteinFastaStep '{self.name}': only {len(records)} sequence(s) found for "
                f"{protein!r} (taxon {taxon_id}); multiple-sequence alignment needs at least 2. "
                f"Broaden the protein filter or feature_type."
            )

        fasta_text = "".join(
            f">{r['id']} {r['product']} | {r['genome_name']}\n{r['sequence']}\n" for r in records
        )
        self.nb_logger.info(
            "BvbrcProteinFastaStep %s: fetched %d %r sequences for taxon %d",
            self.name,
            len(records),
            protein,
            taxon_id,
        )
        return {
            "protein_fasta": {
                "fasta_text": fasta_text,
                "records": records,
                "n_sequences": len(records),
                "taxon_id": taxon_id,
                "protein": protein,
                "feature_type": feature_type,
            }
        }

    def _unwrap(self, input_data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"BvbrcProteinFastaStep '{self.name}': input_data must be a dict, got "
                f"{type(input_data).__name__}"
            )
        # Single-key trigger-envelope unwrap (the framework delivers {du_name: payload}).
        if "taxon_id" not in input_data and len(input_data) == 1:
            only = next(iter(input_data.values()))
            if isinstance(only, dict):
                return only
        return input_data

    # ----- real BV-BRC data-API access (no mocks; FAIL-LOUD on error) -----
    def _fetch(self, taxon_id: int, protein: str, feature_type: str) -> list[dict[str, Any]]:
        features = self._query_features(taxon_id, protein, feature_type)
        md5s = sorted({f["aa_sequence_md5"] for f in features if f.get("aa_sequence_md5")})
        seq_by_md5 = self._query_sequences(md5s)
        records: list[dict[str, Any]] = []
        for f in features:
            md5 = f.get("aa_sequence_md5")
            seq = seq_by_md5.get(md5) if md5 else None
            if not seq:
                # Honest skip — a feature whose sequence the API didn't return is dropped with
                # a warning, never backfilled with a placeholder.
                self.nb_logger.warning(
                    "BvbrcProteinFastaStep %s: no AA sequence for md5=%s (%s); skipping",
                    self.name,
                    md5,
                    f.get("patric_id"),
                )
                continue
            records.append(
                {
                    "id": f.get("patric_id") or md5,
                    "product": f.get("product") or "",
                    "genome_name": f.get("genome_name") or "",
                    "sequence": seq,
                }
            )
        records = self._select_length_cluster(records)
        return records[: self._max_sequences]

    def _select_length_cluster(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep the DOMINANT (most-populous) length cluster; drop outlier-length records.

        BV-BRC's ``product`` substring match for a protein (e.g. ``*envelope*``) pulls in records
        of heterogeneous length: the real target (~495aa envelope E), short partial-genome
        fragments, AND a few mis-annotated/whole polyprotein records (~1180aa). The former
        ``min_length_fraction`` filter anchored its cutoff to the SINGLE LONGEST record, so one
        long outlier (the 1180aa polyprotein) raised the bar to 944aa and collapsed the keep set
        to the outlier alone — leaving <2 sequences and failing MAFFT (dengue's real bug).

        Instead, find the modal length band — the central length whose ±tolerance window contains
        the most records — and keep that band. Outlier-length records (alone in their own sparse
        bands) are dropped, so the dominant ~495aa cluster of envelope proteins survives intact.

        FAIL-LOUD when the dominant band has <2 sequences: that is a genuine named degrade (the
        taxon's deposited sequences are length-heterogeneous with no alignable cohort), not a crash
        to paper over.
        """
        if len(records) < 2:
            # The caller's own <2 guard reports the genuinely-too-few case with full context.
            return records

        lengths = [len(r["sequence"]) for r in records]
        tol = self._length_cluster_tolerance
        # Sliding-window mode: try every observed length as the band center; the band that
        # contains the most records wins. Iterate ascending with ``>=`` so that on a count tie
        # the LARGER center wins — prefer the fuller-length cohort over a shorter-fragment one.
        best_lo = best_hi = None
        best_count = -1
        for center in sorted(set(lengths)):
            lo = center * (1.0 - tol)
            hi = center * (1.0 + tol)
            count = sum(1 for length in lengths if lo <= length <= hi)
            if count >= best_count:
                best_count = count
                best_lo, best_hi = lo, hi

        kept = [r for r in records if best_lo <= len(r["sequence"]) <= best_hi]
        dropped = len(records) - len(kept)
        if dropped:
            kept_lengths = sorted(len(r["sequence"]) for r in kept)
            self.nb_logger.info(
                "BvbrcProteinFastaStep %s: length-cluster (±%.0f%%) kept the dominant %d–%daa "
                "band (%d of %d records); dropped %d outlier-length record(s) (full range "
                "%d–%daa)",
                self.name,
                tol * 100,
                kept_lengths[0],
                kept_lengths[-1],
                len(kept),
                len(records),
                dropped,
                min(lengths),
                max(lengths),
            )
        if len(kept) < 2:
            raise ValueError(
                f"BvbrcProteinFastaStep '{self.name}': length-cluster selection (±{tol:.0%}) found "
                f"no coherent length band with ≥2 sequences among {len(records)} fetched "
                f"(lengths {min(lengths)}–{max(lengths)}aa); the deposited sequences are "
                f"length-heterogeneous (partial genomes / mixed features) with no alignable cohort. "
                f"Broaden the protein/feature_type or widen length_cluster_tolerance."
            )
        return kept

    def _get_json(self, path: str, query: str) -> list[dict[str, Any]]:
        url = f"{self._api_base}/{path}/?{query}&http_accept=application/json"
        resp = requests.get(url, timeout=self._timeout)
        resp.raise_for_status()  # FAIL-LOUD on HTTP error (never silently return [])
        data = resp.json()
        if not isinstance(data, list):
            raise ValueError(
                f"BvbrcProteinFastaStep '{self.name}': unexpected BV-BRC response shape from "
                f"{path}: {type(data).__name__}"
            )
        return data

    def _query_features(
        self, taxon_id: int, protein: str, feature_type: str
    ) -> list[dict[str, Any]]:
        # Over-fetch features (some md5s resolve to no sequence) then cap in _fetch.
        limit = self._max_sequences * 3
        query = (
            f"eq(taxon_id,{taxon_id})"
            f"&eq(feature_type,{feature_type})"
            f"&eq(product,*{protein}*)"
            f"&select(patric_id,product,genome_name,aa_sequence_md5)"
            f"&limit({limit})"
        )
        return self._get_json("genome_feature", query)

    def _query_sequences(self, md5s: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for i in range(0, len(md5s), _MD5_BATCH):
            batch = md5s[i : i + _MD5_BATCH]
            in_list = ",".join(batch)
            query = f"in(md5,({in_list}))&select(md5,sequence)&limit({len(batch)})"
            for row in self._get_json("feature_sequence", query):
                md5 = row.get("md5")
                seq = row.get("sequence")
                if md5 and isinstance(seq, str) and seq:
                    out[md5] = seq
        return out


__all__ = ["BvbrcProteinFastaStep", "BvbrcProteinFastaStepConfig"]
