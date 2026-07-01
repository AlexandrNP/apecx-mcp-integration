"""ProteinNameNormalizationStep — resolve a user protein name to BV-BRC's product term.

The sequence-conservation leg fetches per-strain sequences with BV-BRC's ``eq(product,*<name>*)``
SUBSTRING filter (``BvbrcProteinFastaStep._query_features``). When the user's protein name is not a
contiguous substring of BV-BRC's verbose annotation, the fetch returns <2 sequences and falls
through to the substitute path, which silently aligns a DIFFERENT protein. The classic miss:
``"E2 glycoprotein"`` never substring-matches ``"E2 envelope glycoprotein"`` because ``"envelope"``
sits between the two words.

This step runs BEFORE the fetch and, using the taxon's own BV-BRC product catalog as the authority,
rewrites the protein name to the actual product term when — and ONLY when — the literal name would
fetch nothing:

  1. Query the taxon's product catalog once (CDS + mat_peptide in one request).
  2. FALLBACK GATE — if any product word-boundary-matches the literal name (``_product_matches_word_
     boundary``, the exact predicate the fetch uses to keep/reject a feature), the existing substring
     fetch already works → PASS THROUGH unchanged. This guarantees no behavior change on queries that
     already fetch (including generic names like ``"glycoprotein"`` that legitimately match many
     products and must NOT be pinned to one).
  3. Otherwise pick the best TOKEN-SUBSET product (all the user's whole-word tokens ⊆ the product's
     tokens): ``{e2, glycoprotein} ⊆ {e2, envelope, glycoprotein}`` → ``"E2 envelope glycoprotein"``.
     Token-SET comparison is inherently word-boundary-safe — ``"structural"`` and ``"nonstructural"``
     are distinct tokens, so the "structural ⊂ nonstructural" trap cannot occur here.

SCOPE (v1): fixes the interleaved-modifier case (user's tokens are a subset of the product's). It
does NOT bridge true synonyms / abbreviations (``"spike"`` → ``"surface glycoprotein"``, ``"NS3"`` →
``"nonstructural protein 3"``) — those need a curated alias dict (deferred). When no token-subset
product clears the threshold, the step passes through and the existing fetch/substitute path runs
exactly as before.

DEGRADE-LOUD, NEVER RAISE: normalization is an enhancement, not a gate. Missing/invalid params, an
empty catalog, or a BV-BRC error all fall back to the ORIGINAL name (logged) so the downstream fetch
behaves exactly as it would without this step.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import requests
from nanobrain.core.step import BaseStep, StepConfig
from pydantic import Field

from apecx_integration.composition.steps.bvbrc_protein_fasta_step import (
    _product_matches_word_boundary,
)

log = logging.getLogger(__name__)

# Bounds the client-side product-catalog scan (one lightweight product+feature_type query).
_CATALOG_CAP = 5000


def _token_subset_score(product: str, user_protein: str) -> float:
    """Fraction of the user's whole-word tokens present in the product's token set (0.0–1.0).

    1.0 means every token the user typed appears (as a whole word) in the product name — i.e. the
    user's name is a token-subset of the product. Tokens are alphanumeric runs, case-folded, so
    ``"structural"`` and ``"nonstructural"`` are distinct (no substring bleed). Empty user → 0.0.
    """
    user_toks = set(re.findall(r"[A-Za-z0-9]+", user_protein.lower()))
    prod_toks = set(re.findall(r"[A-Za-z0-9]+", product.lower()))
    if not user_toks:
        return 0.0
    return len(user_toks & prod_toks) / len(user_toks)


class ProteinNameNormalizationStepConfig(StepConfig):
    """Config for BV-BRC protein-name normalization.

    Inherits StepConfig's ``extra='allow'`` (the framework injects metadata like ``source_path`` at
    load time) — a StepConfig subclass must NOT set ``extra='forbid'``, unlike a plain data model.
    """

    bvbrc_api_base: str = Field(default="https://www.bv-brc.org/api")
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    feature_types: tuple[str, ...] = Field(
        default=("CDS", "mat_peptide"),
        description="BV-BRC feature types scanned for candidate products. CDS covers whole-CDS "
        "annotations; mat_peptide covers the mature proteins of polyprotein viruses.",
    )
    min_match_score: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Minimum token-subset score to accept a normalized product. 1.0 (default) "
        "requires ALL the user's tokens to appear in the product name.",
    )


class ProteinNameNormalizationStep(BaseStep):
    """Rewrite a user protein name to the BV-BRC product term for a taxon; degrade-loud passthrough."""

    COMPONENT_TYPE = "protein_name_normalization_step"

    @classmethod
    def _get_config_class(cls):
        return ProteinNameNormalizationStepConfig

    def _init_from_config(self, config, component_config, dependencies) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._api_base: str = getattr(config, "bvbrc_api_base", "https://www.bv-brc.org/api")
        self._timeout: float = float(getattr(config, "request_timeout_seconds", 30.0))
        self._feature_types: tuple[str, ...] = tuple(
            getattr(config, "feature_types", ("CDS", "mat_peptide"))
        )
        self._min_match_score: float = float(getattr(config, "min_match_score", 1.0))

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        import asyncio

        payload = self._unwrap(input_data)
        taxon_id = payload.get("taxon_id")
        protein = payload.get("protein")
        feature_type = payload.get("feature_type")  # may be absent; None → fetch step's own default

        # Unusable params: pass the original payload straight through (the fetch step raises its own
        # clear error). Normalization never introduces a failure the caller wouldn't already have.
        if not (isinstance(taxon_id, int) or (isinstance(taxon_id, str) and taxon_id.isdigit())):
            log.warning(
                "ProteinNameNormalizationStep %s: no usable taxon_id (%r); passthrough",
                self.name,
                taxon_id,
            )
            return self._emit(taxon_id, protein, feature_type, protein, "passthrough")
        if not (isinstance(protein, str) and protein.strip()):
            log.warning(
                "ProteinNameNormalizationStep %s: no usable protein (%r); passthrough",
                self.name,
                protein,
            )
            return self._emit(taxon_id, protein, feature_type, protein, "passthrough")
        taxon_id = int(taxon_id)
        protein = protein.strip()

        try:
            catalog = await asyncio.to_thread(self._query_catalog, taxon_id)
        except Exception as exc:  # noqa: BLE001 — normalization must NEVER break the fetch
            log.warning(
                "ProteinNameNormalizationStep %s: BV-BRC catalog query failed for taxon %d (%s); "
                "passthrough",
                self.name,
                taxon_id,
                exc,
            )
            return self._emit(taxon_id, protein, feature_type, protein, "passthrough")

        if not catalog:
            log.info(
                "ProteinNameNormalizationStep %s: no products found for taxon %d; passthrough",
                self.name,
                taxon_id,
            )
            return self._emit(taxon_id, protein, feature_type, protein, "passthrough")

        # Fallback gate: if the literal name already word-boundary-matches an available product, the
        # existing substring fetch will find it — do NOT second-guess a query that already works.
        if any(_product_matches_word_boundary(prod, protein) for (prod, _ft) in catalog):
            log.debug(
                "ProteinNameNormalizationStep %s: %r already matches an available product for "
                "taxon %d; passthrough",
                self.name,
                protein,
                taxon_id,
            )
            return self._emit(taxon_id, protein, feature_type, protein, "passthrough")

        # No literal match — pick the best token-subset product. Order by score desc, then BV-BRC
        # feature count desc (frequency signal), then product name asc (deterministic tie-break).
        scored = [
            (_token_subset_score(prod, protein), count, prod, ft)
            for (prod, ft), count in catalog.items()
        ]
        scored.sort(key=lambda t: (-t[0], -t[1], t[2]))
        best_score, best_count, best_product, best_ft = scored[0]

        if best_score >= self._min_match_score:
            log.info(
                "ProteinNameNormalizationStep %s: normalized %r → %r (%s, score=%.2f, n=%d) for "
                "taxon %d",
                self.name,
                protein,
                best_product,
                best_ft,
                best_score,
                best_count,
                taxon_id,
            )
            return self._emit(taxon_id, best_product, best_ft, protein, "bvbrc_token_subset")

        log.info(
            "ProteinNameNormalizationStep %s: no token-subset product ≥ %.2f for %r (taxon %d, best "
            "%r score=%.2f); passthrough",
            self.name,
            self._min_match_score,
            protein,
            taxon_id,
            best_product,
            best_score,
            taxon_id,
        )
        return self._emit(taxon_id, protein, feature_type, protein, "passthrough")

    @staticmethod
    def _emit(
        taxon_id: Any,
        protein: Any,
        feature_type: str | None,
        original_protein: Any,
        match_source: str,
    ) -> dict[str, Any]:
        """Build the ``{"norm_out": {...}}`` output the fetch step consumes.

        Always carries ``taxon_id`` / ``protein`` (the fetch-ready payload) plus disclosure keys
        ``original_protein`` (the user's term), ``match_source``, and ``product_exact``.
        ``feature_type`` is included only when normalization picked one OR it was present on input —
        a plain passthrough must not inject a spurious ``"CDS"`` that would override the fetch step's
        own default.

        ``product_exact`` is True only when ``protein`` is a VERBATIM BV-BRC product name resolved
        from the catalog (``match_source == "bvbrc_token_subset"``). It signals ``BvbrcProteinFastaStep``
        to retrieve with an EXACT ``eq(product,"…")`` query instead of the wildcard ``eq(product,*…*)``:
        BV-BRC's Solr product field returns 0 for a wildcarded multi-word phrase like
        ``*E2 envelope glycoprotein*`` even though the exact product exists, so a resolved verbose
        name is only retrievable via exact match. On passthrough the flag is absent → the fetch keeps
        its current wildcard behavior (zero regression). The fetch reads only ``taxon_id`` /
        ``protein`` / ``feature_type`` / ``product_exact``; other keys stay inert for debugging.
        """
        out: dict[str, Any] = {
            "taxon_id": taxon_id,
            "protein": protein,
            "original_protein": original_protein,
            "match_source": match_source,
            "product_exact": match_source == "bvbrc_token_subset",
        }
        if feature_type is not None:
            out["feature_type"] = feature_type
        return {"norm_out": out}

    def _unwrap(self, input_data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"ProteinNameNormalizationStep '{self.name}': input_data must be a dict, got "
                f"{type(input_data).__name__}"
            )
        # Single-key trigger-envelope unwrap (the framework delivers {du_name: payload}).
        if "taxon_id" not in input_data and len(input_data) == 1:
            only = next(iter(input_data.values()))
            if isinstance(only, dict):
                return only
        return input_data

    # ----- real BV-BRC data-API access (no mocks; raise on error, caller degrades to passthrough) --
    def _query_catalog(self, taxon_id: int) -> dict[tuple[str, str], int]:
        """Return ``{(product, feature_type): count}`` for the taxon across the configured feature
        types, from ONE BV-BRC ``genome_feature`` query (both types via ``in()``), counted
        client-side. Rows carry just ``product`` + ``feature_type`` (lightweight). Raises on
        HTTP/parse error — ``process`` catches and degrades to passthrough.
        """
        ft_list = ",".join(self._feature_types)
        query = (
            f"eq(taxon_id,{taxon_id})"
            f"&in(feature_type,({ft_list}))"
            f"&select(product,feature_type)"
            f"&limit({_CATALOG_CAP})"
        )
        rows = self._get_json("genome_feature", query)
        counts: dict[tuple[str, str], int] = {}
        for r in rows:
            product = (r.get("product") or "").strip()
            ft = (r.get("feature_type") or "").strip()
            if product and ft:
                counts[(product, ft)] = counts.get((product, ft), 0) + 1
        return counts

    def _get_json(self, path: str, query: str) -> list[dict[str, Any]]:
        url = f"{self._api_base}/{path}/?{query}&http_accept=application/json"
        resp = requests.get(url, timeout=self._timeout)
        resp.raise_for_status()  # raise on HTTP error (never silently return [])
        data = resp.json()
        if not isinstance(data, list):
            raise ValueError(
                f"ProteinNameNormalizationStep '{self.name}': unexpected BV-BRC response shape "
                f"from {path}: {type(data).__name__}"
            )
        return data


__all__ = ["ProteinNameNormalizationStep", "ProteinNameNormalizationStepConfig"]
