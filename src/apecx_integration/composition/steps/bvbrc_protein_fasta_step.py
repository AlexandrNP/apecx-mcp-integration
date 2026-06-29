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
import re
from typing import Any

import requests
from nanobrain.core.step import BaseStep, StepConfig
from pydantic import Field


def _product_matches_word_boundary(product: str, protein: str) -> bool:
    """True iff ``protein`` occurs in ``product`` at a WORD BOUNDARY — i.e. not as a
    substring of a longer word.

    The BV-BRC query uses ``eq(product,*{protein}*)`` (a wildcard SUBSTRING match), so a
    query for ``"structural polyprotein"`` ALSO matches ``"nonstructural polyprotein"``
    (the query term is literally a substring). That silently aligned the WRONG protein
    (verified 2026-06-13: CHIKV was aligning the nonstructural polyprotein). This filter
    requires the match to start at a word boundary (start-of-string or a non-alphanumeric
    char before it) AND not be preceded by the negation prefix ``non``/``non-`` — so
    ``"structural"`` matches neither ``"nonstructural"`` (no boundary) nor
    ``"non-structural"`` (hyphen would otherwise read as a boundary). Short domain/protein
    tags ("E1", "capsid", "E" in "prM-E") at a real word start still match.
    """
    if not product:
        return False
    pat = r"(?<![A-Za-z0-9])(?<!non)(?<!non-)" + re.escape(protein.strip())
    return re.search(pat, product, re.IGNORECASE) is not None


# Non-informative BV-BRC product names. Substituting the sequence-conservation analysis to one of
# these (the too-few-sequences fallback) is meaningless — e.g. taxon 126283 "Herpes simplex virus
# unknown type" is poorly annotated and its most-covered product is "unnamed protein product", so a
# request for "thymidine kinase" auto-substituted that JUNK name (2026-06-27 probe). Better to degrade
# loud ("no informative alternate") than to label a conservation plot "unnamed protein product". Only
# the GENERIC catch-all names are rejected; a named-but-putative product ("putative ORF1ab polyprotein")
# is still informative and kept.
_UNINFORMATIVE_PRODUCT_RE = re.compile(
    r"^(unnamed protein product|hypothetical protein|uncharacteri[sz]ed protein|"
    r"unknown protein|predicted protein|putative protein|protein|product)\s*$",
    re.IGNORECASE,
)


def _is_informative_product(product: str) -> bool:
    """True iff ``product`` is a SPECIFIC protein name (not a generic catch-all annotation) usable as
    a conservation-leg substitute. Empty / generic ("unnamed protein product", "hypothetical protein")
    → False."""
    p = (product or "").strip()
    return bool(p) and _UNINFORMATIVE_PRODUCT_RE.match(p) is None


log = logging.getLogger(__name__)

# feature_sequence batched lookups — keep each request URL well under server limits.
_MD5_BATCH = 40
# Cap on alternate products tried by the too-few-sequences fallback (bounds network calls).
_MAX_SUBSTITUTE_ATTEMPTS = 3


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

        records, n_fetched, n_dropped_length_outlier = await asyncio.to_thread(
            self._fetch, taxon_id, protein, feature_type
        )

        requested_protein = protein
        substituted_protein: str | None = None
        if len(records) < 2 and feature_type != "mat_peptide":
            # Mature-peptide retry (2026-06-29): a mature protein of a POLYPROTEIN virus (alphavirus
            # capsid / E1 / E2 / 6K, flavivirus envelope, ...) is annotated as a mat_peptide feature,
            # NOT a CDS — the CDS is the whole polyprotein — so the CDS fetch for the mature protein
            # finds <2. Retry the SAME protein as mat_peptide BEFORE substituting a DIFFERENT product:
            # this gives REAL per-mature-protein conservation (e.g. EEEV "capsid protein" → 0 CDS but
            # ~1200 mat_peptide). NOTE: a short request whose name is not a substring of the verbose
            # BV-BRC product ("E2 glycoprotein" vs "E2 envelope glycoprotein"; "NS3" vs "nonstructural
            # protein 3") still misses here — protein-name normalization is a separate follow-up
            # (recorded in docs/fresh_install_findings.md).
            mp_records, mp_fetched, mp_dropped = await asyncio.to_thread(
                self._fetch, taxon_id, protein, "mat_peptide"
            )
            if len(mp_records) >= 2:
                log.info(
                    "BvbrcProteinFastaStep %s: %r had <2 %s feature(s) for taxon %d; using %d "
                    "mat_peptide feature(s) for the SAME protein (mature-peptide conservation).",
                    self.name,
                    requested_protein,
                    feature_type,
                    taxon_id,
                    len(mp_records),
                )
                records = mp_records
                n_fetched, n_dropped_length_outlier = mp_fetched, mp_dropped
                feature_type = "mat_peptide"

        if len(records) < 2:
            # Too-few-sequences fallback: reverse-lookup the products that DO exist for this taxon,
            # and auto-retry the most-covered one (>=2). The substitute fixes ONLY the sequence-
            # conservation leg; the structural/functional legs still use the requested protein —
            # the divergence is surfaced loudly downstream (SequenceEvidenceMergeStep proceed_note).
            available = await asyncio.to_thread(
                self._query_available_proteins, taxon_id, feature_type
            )
            # Try the most-covered alternates in order until one yields >=2 USABLE sequences after
            # the word-boundary filter + length-cluster cull (feature count >=2 does not guarantee
            # >=2 alignable). Bounded to keep network calls in check.
            candidates = [
                (p, c)
                for p, c in available
                if p.strip().lower() != requested_protein.lower()
                and c >= 2
                and _is_informative_product(p)  # never substitute to "unnamed protein product" junk
            ]
            for cand_protein, cand_count in candidates[:_MAX_SUBSTITUTE_ATTEMPTS]:
                sub_records, sub_fetched, sub_dropped = await asyncio.to_thread(
                    self._fetch, taxon_id, cand_protein, feature_type
                )
                if len(sub_records) >= 2:
                    log.warning(
                        "BvbrcProteinFastaStep %s: %r had <2 sequences for taxon %d; auto-"
                        "substituting product %r (%d features)",
                        self.name,
                        requested_protein,
                        taxon_id,
                        cand_protein,
                        cand_count,
                    )
                    records = sub_records
                    n_fetched, n_dropped_length_outlier = sub_fetched, sub_dropped
                    substituted_protein = cand_protein
                    protein = cand_protein
                    break
            if len(records) < 2:
                avail_str = (
                    ", ".join(f"{p} (n={c})" for p, c in available[:10]) if available else "none"
                )
                raise ValueError(
                    f"BvbrcProteinFastaStep '{self.name}': only {len(records)} sequence(s) for "
                    f"{requested_protein!r} (taxon {taxon_id}) and no alternate product has >=2 "
                    f"sequences (alignment needs at least 2). Available products for this taxon: "
                    f"{avail_str}. Re-run with one of these as the protein, or verify the taxon."
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
                # Fetched-vs-used disclosure: how many BV-BRC records resolved to a sequence
                # (n_fetched) and how many were culled as length outliers before alignment.
                "n_fetched": n_fetched,
                "n_dropped_length_outlier": n_dropped_length_outlier,
                "taxon_id": taxon_id,
                "protein": protein,
                # Fallback disclosure: the originally-requested protein and the auto-selected
                # substitute (None when no substitution happened). Threaded downstream so the
                # merge step can surface the substitution + its scope caveat.
                "requested_protein": requested_protein,
                "substituted_protein": substituted_protein,
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
    def _fetch(
        self, taxon_id: int, protein: str, feature_type: str
    ) -> tuple[list[dict[str, Any]], int, int]:
        """Return ``(records, n_fetched, n_dropped_length_outlier)``.

        ``n_fetched`` = records that resolved to an AA sequence before the length-cluster cull;
        ``n_dropped_length_outlier`` = records culled as length outliers. Both feed the report's
        fetched-vs-used disclosure.
        """
        features = self._query_features(taxon_id, protein, feature_type)
        # The BV-BRC query is a SUBSTRING match (eq(product,*X*)); drop records where the
        # protein term only matches mid-word (e.g. "structural" inside "nonstructural
        # polyprotein") so we never silently align the wrong protein. If the boundary
        # filter would drop EVERY record, fall back to the unfiltered set + a loud warning
        # (the protein name may legitimately only appear mid-product for this taxon).
        bounded = [
            f for f in features if _product_matches_word_boundary(f.get("product") or "", protein)
        ]
        if bounded:
            if len(bounded) < len(features):
                log.info(
                    "BvbrcProteinFastaStep %s: word-boundary filter dropped %d/%d %r features "
                    "(substring-only matches, e.g. 'nonstructural')",
                    self.name,
                    len(features) - len(bounded),
                    len(features),
                    protein,
                )
            features = bounded
        elif features:
            log.warning(
                "BvbrcProteinFastaStep %s: protein %r matched %d feature(s) only MID-WORD "
                "(no word-boundary match); using them but the product names may not be the "
                "intended protein — check the product name.",
                self.name,
                protein,
                len(features),
            )
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
        n_with_sequence = len(records)  # records that resolved to an AA sequence (pre-cull)
        records, n_dropped = self._select_length_cluster(records)
        return records[: self._max_sequences], n_with_sequence, n_dropped

    def _select_length_cluster(
        self, records: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], int]:
        """Keep the DOMINANT (most-populous) length cluster; drop outlier-length records.

        Returns ``(kept, n_dropped)`` — the dropped count feeds the report's fetched-vs-used
        disclosure (how many records were culled as length outliers before alignment).

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
            return records, 0

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
        return kept, dropped

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

    def _query_available_proteins(
        self, taxon_id: int, feature_type: str, *, cap: int = 5000
    ) -> list[tuple[str, int]]:
        """Reverse lookup: which protein products (and how many features each) exist for this taxon.

        Drops the product filter and counts the ``product`` field client-side (BV-BRC RQL has no
        portable facet here). Returns ``[(product, count), …]`` sorted by count desc. ``cap`` bounds
        the scan; a taxon with more features than ``cap`` yields counts over the scanned window
        (still a useful "what's available" signal). Network/parse errors degrade to ``[]`` (the
        caller's raise carries its own guidance) — never raises.
        """
        query = (
            f"eq(taxon_id,{taxon_id})&eq(feature_type,{feature_type})&select(product)&limit({cap})"
        )
        try:
            rows = self._get_json("genome_feature", query)
        except Exception:  # noqa: BLE001 - best-effort guidance; the caller raises with context
            return []
        counts: dict[str, int] = {}
        for r in rows:
            product = (r.get("product") or "").strip()
            if product:
                counts[product] = counts.get(product, 0) + 1
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))

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
