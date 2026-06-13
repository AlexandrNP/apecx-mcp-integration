"""Shared async HTTP-with-retry base for the functional-annotation clients.

Factored out of the OLSClient pattern (``synonym_dictionary.ols_client``) so the three
E3-3 clients share one retry/backoff/lifecycle implementation rather than copying it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE_SECONDS = 1.0


class HttpClientError(RuntimeError):
    """Raised when a request fails after all retries (carries the final HTTP status)."""


class AsyncHttpClient:
    """Async httpx wrapper with retry/backoff and context-manager lifecycle.

    Subclasses set ``_label`` (for log lines) and call :meth:`_get_json`. Use as an async
    context manager so the connection pool is closed:

    .. code-block:: python

        async with SiftsClient() as c:
            mappings = await c.get_mappings("2xfb")
    """

    _label = "http"

    def __init__(
        self,
        *,
        base_url: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
        backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._retry_attempts = retry_attempts
        self._backoff_base = backoff_base
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> AsyncHttpClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """GET ``base_url + path`` with retry; return parsed JSON.

        404 raises :class:`HttpClientError` with ``"404"`` in the message so callers can
        treat "no such record" as a named absence rather than a hard failure. Transient
        429/503/5xx are retried with exponential backoff. Network errors are retried then
        surfaced as :class:`HttpClientError`.
        """
        body, _ = await self._get_json_and_headers(path, params=params)
        return body

    async def _get_json_and_headers(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> tuple[Any, dict[str, str]]:
        """Like :meth:`_get_json` but also returns the response headers (lower-cased keys).

        Needed where provenance lives in a header rather than the body (e.g. UniProt's
        ``x-uniprot-release``).
        """
        url = f"{self._base_url}{path}"
        last_status: int | None = None
        last_text = ""
        for attempt in range(1, self._retry_attempts + 1):
            try:
                resp = await self._client.get(url, params=params)
            except httpx.HTTPError as exc:
                log.warning("%s request failed (attempt %d): %s", self._label, attempt, exc)
                if attempt >= self._retry_attempts:
                    raise HttpClientError(f"{self._label} request failed: {exc}") from exc
                await self._backoff(attempt)
                continue

            last_status = resp.status_code
            if resp.status_code == 404:
                raise HttpClientError(f"{self._label} 404 for {url} params={params}")
            if resp.status_code in (429, 503):
                if attempt < self._retry_attempts:
                    await self._backoff(attempt)
                    continue
                last_text = resp.text[:200]
                break
            if resp.is_error:
                last_text = resp.text[:200]
                if attempt < self._retry_attempts:
                    await self._backoff(attempt)
                    continue
                break
            return resp.json(), {k.lower(): v for k, v in resp.headers.items()}

        raise HttpClientError(
            f"{self._label} gave up after {self._retry_attempts} attempts: "
            f"last status={last_status}, body[:200]={last_text!r}"
        )

    async def _backoff(self, attempt: int) -> None:
        await asyncio.sleep(self._backoff_base * (2 ** (attempt - 1)))


__all__ = ["AsyncHttpClient", "HttpClientError"]
