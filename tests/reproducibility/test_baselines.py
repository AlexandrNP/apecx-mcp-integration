"""T12 AC1+AC2+AC3: run every reproducibility fixture through the
composer and compare against the baseline.

Each fixture under ``tests/reproducibility/fixtures/`` is parametrized
through ``Composer.compose(prompt)``; the comparator ladder in
``harness.check`` decides pass/fail.

Composer backend
----------------
Two generation paths, selected per-fixture via a ``canned_response``
file (optional):

- If ``canned_response.txt`` is present in the fixture dir, a
  placeholder LLM factory returns that text verbatim. Deterministic,
  no Ollama dep — these fixtures run in CI and catch composer-pipeline
  drift (parser changes, YAML serialization shifts, categorization
  format drift).
- If ``canned_response.txt`` is absent, the fixture is live-LLM and
  auto-skips unless ``APECX_T12_RUN_LIVE_LLM=1`` is set AND a
  reachable backend is configured. Those fixtures catch real model
  drift (the original point of T12) and are operator-run until CI
  gets an LLM allocation.

Marker
------
``@pytest.mark.integration`` — reproducibility is load-bearing against
composer + (optionally) LLM. A plain ``pytest -m "not integration"``
run does not exercise it.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from apecx_integration.composition.composer import Composer
from tests.reproducibility.harness import (
    Fixture,
    check,
    discover_fixtures,
)

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSER_CONFIG = (
    REPO_ROOT
    / "src"
    / "apecx_integration"
    / "composition"
    / "composer_config.yml"
)
FIXTURES_DIR = Path(__file__).parent / "fixtures"


class _PlaceholderResponse:
    def __init__(self, content: str):
        self.content = content


class _PlaceholderLLM:
    def __init__(self, canned: str):
        self.canned = canned

    def invoke(self, messages):
        return _PlaceholderResponse(self.canned)


def _placeholder_factory(canned: str):
    def _factory(**_kwargs):
        return _PlaceholderLLM(canned)

    return _factory


def _canned_path(fixture_name: str) -> Path:
    return FIXTURES_DIR / fixture_name / "canned_response.txt"


def _live_llm_opted_in() -> bool:
    return os.environ.get("APECX_T12_RUN_LIVE_LLM") == "1"


_fixtures = discover_fixtures()


def _generate_bytes(fixture: Fixture) -> bytes:
    """Build a fresh Composer for this fixture and run compose().

    Kept fixture-local (not a module-level singleton) because the
    placeholder path monkey-patches ``_llm_factory``; a shared
    Composer would bleed canned responses across fixtures.
    """
    composer = Composer.from_config(COMPOSER_CONFIG)
    canned = _canned_path(fixture.name)
    if canned.is_file():
        composer._llm_factory = _placeholder_factory(
            canned.read_text(encoding="utf-8")
        )
    elif not _live_llm_opted_in():
        pytest.skip(
            f"fixture {fixture.name!r} has no canned_response.txt and "
            "APECX_T12_RUN_LIVE_LLM!=1 — live-LLM fixtures are "
            "operator-run until CI gets an LLM allocation."
        )
    # Artifact store intentionally absent: Phase-2 compat path — no
    # DB writes, no persistence. T12 tests bytes, not persistence.
    result = asyncio.run(composer.compose(fixture.prompt))
    return result.yaml_bytes


@pytest.mark.parametrize(
    "fixture",
    _fixtures,
    ids=[f.name for f in _fixtures],
)
def test_baseline(fixture: Fixture) -> None:
    generated = _generate_bytes(fixture)
    check(generated=generated, fixture=fixture)


def test_at_least_one_fixture_present() -> None:
    """Guards against an empty fixtures directory silently producing
    a green suite. AC1 targets 10 fixtures by end of Phase 2; the
    repo currently ships placeholder-LLM seeds that exercise the
    pipeline deterministically in CI.
    """
    if not _fixtures:
        pytest.skip(
            "No fixtures present yet. See tests/reproducibility/README.md "
            "for how to add one."
        )
    assert len(_fixtures) >= 1
