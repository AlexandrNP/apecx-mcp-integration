"""EBI OLS (Ontology Lookup Service) HTTP client.

The Stage 1 builder hits OLS in two modes:

1. **Anchor-mode** (dominant per M1 measurements): a row already has a
   canonical IRI from a database column (e.g. ``NCBI_Taxonomy_ID``).
   Fetch the term's preferred label + synonyms array given the IRI.
   This is :meth:`OLSClient.get_term`.
2. **Search-mode** (fallback for un-IDd rows): query a free-text
   surface form against an ontology, get back ranked candidates.
   This is :meth:`OLSClient.search`.

Both mode results are **cached in-process** for the duration of a build
to coalesce the many duplicate lookups that occur across rows referring
to the same entity (e.g. 3258 BV-BRC genome rows that share NCBITaxon
37124 trigger one OLS call, not 3258).

This module is async (httpx.AsyncClient) so the Stage 1 transform can
fan-out resolutions concurrently when run against larger datasets at
Phase 5/6.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from apecx_integration.synonym_dictionary.enums import OntologyName

log = logging.getLogger(__name__)


# Public OLS v4 endpoint.
DEFAULT_BASE_URL = "https://www.ebi.ac.uk/ols4/api"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE_SECONDS = 1.0
DEFAULT_INTER_REQUEST_DELAY_SECONDS = 0.0


class OLSError(RuntimeError):
    """Raised when an OLS request fails after all retries."""


class OLSClient:
    """Async client for the EBI OLS v4 API.

    Use as an async context manager so the underlying httpx connection
    pool is closed cleanly:

    .. code-block:: python

        async with OLSClient() as client:
            term = await client.get_term(
                ontology=OntologyName.NCBITAXON,
                iri="http://purl.obolibrary.org/obo/NCBITaxon_37124",
            )
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
        backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS,
        inter_request_delay: float = DEFAULT_INTER_REQUEST_DELAY_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._retry_attempts = retry_attempts
        self._backoff_base = backoff_base
        self._inter_request_delay = inter_request_delay
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        # In-process per-build caches.  Key shapes documented at use site.
        self._term_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
        self._search_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}

    async def __aenter__(self) -> OLSClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # search — for un-IDd rows
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        ontology: OntologyName,
        *,
        rows: int = 5,
        exact: bool = False,
    ) -> list[dict[str, Any]]:
        """Search ``ontology`` for term matches against ``query``.

        Returns the OLS ``response.docs`` array (possibly empty).
        Cached per ``(query, ontology)``.
        """
        cache_key = (query, ontology.value)
        if cache_key in self._search_cache:
            return self._search_cache[cache_key]

        params: dict[str, Any] = {
            "q": query,
            "ontology": ontology.value,
            "rows": rows,
        }
        if exact:
            params["exact"] = "true"

        body = await self._get_json("/search", params=params)
        docs = body.get("response", {}).get("docs", []) or []
        self._search_cache[cache_key] = docs
        return docs

    # ------------------------------------------------------------------
    # get_term — for ID-anchored rows
    # ------------------------------------------------------------------

    async def get_term(
        self,
        ontology: OntologyName,
        iri: str,
    ) -> dict[str, Any] | None:
        """Fetch a term by IRI from the named ontology.

        Returns the OLS ``_embedded.terms[0]`` payload, or ``None`` when the
        IRI is not found in the ontology (404 is treated as None, not an
        error — IRIs from older snapshots may be deprecated).
        """
        cache_key = (ontology.value, iri)
        if cache_key in self._term_cache:
            return self._term_cache[cache_key]

        # OLS expects URL-encoded IRI.  httpx handles encoding via params.
        path = f"/ontologies/{ontology.value}/terms"
        params = {"iri": iri}
        try:
            body = await self._get_json(path, params=params)
        except OLSError as exc:
            # The wrapped error wraps a final HTTP status; treat 404 as
            # "no such term" rather than a hard failure.
            if "404" in str(exc):
                self._term_cache[cache_key] = None
                return None
            raise

        terms = body.get("_embedded", {}).get("terms", []) or []
        result = terms[0] if terms else None
        self._term_cache[cache_key] = result
        return result

    @staticmethod
    def extract_synonyms(term: dict[str, Any] | None) -> tuple[str, ...]:
        """Best-effort synonyms-array extraction from an OLS term payload.

        OLS exposes synonyms in multiple keys depending on the source
        ontology (``synonyms``, ``annotation.has_exact_synonym``, etc.).
        We collect the union, dedupe, and preserve insertion order.
        """
        if not term:
            return ()
        seen: dict[str, None] = {}
        # Top-level "synonyms" — common case.
        for s in term.get("synonyms", []) or []:
            if isinstance(s, str) and s and s not in seen:
                seen[s] = None
        # OBO-style annotations — present for many OBO ontologies.
        annotations = term.get("annotation", {}) or {}
        for key in ("has_exact_synonym", "has_related_synonym", "alternative_term"):
            for s in annotations.get(key, []) or []:
                if isinstance(s, str) and s and s not in seen:
                    seen[s] = None
        return tuple(seen)

    @staticmethod
    def extract_label(term: dict[str, Any] | None) -> str | None:
        if not term:
            return None
        label = term.get("label")
        if isinstance(label, str) and label:
            return label
        return None

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    async def _get_json(self, path: str, *, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        last_status: int | None = None
        last_text = ""
        for attempt in range(1, self._retry_attempts + 1):
            if self._inter_request_delay > 0:
                await asyncio.sleep(self._inter_request_delay)
            try:
                resp = await self._client.get(url, params=params)
            except httpx.HTTPError as exc:
                log.warning("OLS request failed (attempt %d): %s", attempt, exc)
                if attempt >= self._retry_attempts:
                    raise OLSError(f"OLS request failed: {exc}") from exc
                await self._backoff(attempt)
                continue

            last_status = resp.status_code
            if resp.status_code == 404:
                # Surface as OLSError with status; caller may swallow it.
                raise OLSError(f"OLS 404 for {url} params={params}")
            if resp.status_code in (429, 503):
                # Rate-limited or temporarily unavailable; honour Retry-After
                # if present, otherwise exponential backoff.
                retry_after = self._parse_retry_after(resp)
                if attempt < self._retry_attempts:
                    log.info(
                        "OLS %d on attempt %d; retrying after %.2fs",
                        resp.status_code,
                        attempt,
                        retry_after,
                    )
                    await asyncio.sleep(retry_after)
                    continue
                last_text = resp.text[:200]
                break
            if resp.is_error:
                last_text = resp.text[:200]
                if attempt < self._retry_attempts:
                    await self._backoff(attempt)
                    continue
                break
            return resp.json()

        raise OLSError(
            f"OLS request gave up after {self._retry_attempts} attempts: "
            f"last status={last_status}, body[:200]={last_text!r}"
        )

    async def _backoff(self, attempt: int) -> None:
        delay = self._backoff_base * (2 ** (attempt - 1))
        await asyncio.sleep(delay)

    @staticmethod
    def _parse_retry_after(resp: httpx.Response) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after is None:
            return 1.0
        try:
            return max(0.1, float(retry_after))
        except ValueError:
            return 1.0
