"""Proof-of-integration: apecx transforms + nanobrain TransformLink.

Each test loads a ``TransformLink`` from an inline YAML-shaped dict
whose ``transform_function`` points at a real transform in
``apecx_integration.composition.transforms``, wires a fake target into
the link, and verifies the reshaped data lands on the target.

This is the end-to-end test for the nanobrain TransformLink YAML fix
(2026-04-23) + the apecx-side transforms module. If future workflows
add more transforms, the same test shape proves the wiring still works.
"""

from __future__ import annotations

import asyncio

import pytest
from nanobrain.core.link import TransformLink

from apecx_integration.composition.transforms import (
    entities_to_query_terms,
    llm_proposals_to_approved_mappings,
)

pytestmark = pytest.mark.integration


class _FakeTarget:
    """Minimal stand-in for a Step target — TransformLink.transfer()
    writes via ``target.set_input`` when that method is present."""

    def __init__(self) -> None:
        self.received: list = []

    async def set_input(self, data) -> None:
        self.received.append(data)


def _make_link(transform_function: str) -> TransformLink:
    """Build a TransformLink from an inline dict, mimicking what a
    workflow YAML's ``links:`` block would produce."""
    return TransformLink.from_config({
        "link_type": "transform",
        "source": "src_step.out",
        "target": "dst_step.in",
        "transform_function": transform_function,
    })


# ---------------------------------------------------------------------------
# Unit: the transforms themselves
# ---------------------------------------------------------------------------

def test_entities_to_query_terms_prefers_query_terms_key():
    out = entities_to_query_terms({
        "entities": [{"name": "ignored"}],
        "query_terms": ["EEEV", "VEEV"],
    })
    assert out == {"query_terms": ["EEEV", "VEEV"]}


def test_entities_to_query_terms_falls_back_to_entity_names():
    out = entities_to_query_terms({
        "entities": [
            {"name": "EEEV", "type": "pathogen", "confidence": 0.9},
            {"name": "vaccine", "type": "vaccine", "confidence": 0.7},
            {"type": "medical_term", "confidence": 0.8},  # no 'name' — skipped
        ],
    })
    assert out == {"query_terms": ["EEEV", "vaccine"]}


def test_entities_to_query_terms_handles_empty_input():
    assert entities_to_query_terms({}) == {"query_terms": []}


def test_llm_proposals_to_approved_mappings_renames_keys():
    out = llm_proposals_to_approved_mappings({
        "llm_proposals": [
            {"query_entity": "EEEV", "synonym": "EEEV stub strain", "score": 0.92},
            {"query_entity": "VEEV", "synonym": "VEEV stub strain", "score": 0.88},
        ],
    })
    assert out == {
        "approved_mappings": [
            {"query_term": "EEEV", "canonical_term": "EEEV stub strain",
             "confidence": 0.92, "source_run_id": None, "comment": None},
            {"query_term": "VEEV", "canonical_term": "VEEV stub strain",
             "confidence": 0.88, "source_run_id": None, "comment": None},
        ],
    }


def test_llm_proposals_to_approved_mappings_passes_reviewer_metadata():
    out = llm_proposals_to_approved_mappings({
        "llm_proposals": [
            {
                "query_entity": "EEEV", "synonym": "X", "score": 1.0,
                "source_run_id": "abc-123", "comment": "operator said so",
            },
        ],
    })
    assert out["approved_mappings"][0]["source_run_id"] == "abc-123"
    assert out["approved_mappings"][0]["comment"] == "operator said so"


# ---------------------------------------------------------------------------
# End-to-end: TransformLink + transforms + fake target
# ---------------------------------------------------------------------------

def test_transform_link_entities_to_query_terms_end_to_end():
    link = _make_link(
        "apecx_integration.composition.transforms.entities_to_query_terms"
    )
    target = _FakeTarget()
    link.target = target

    async def run():
        await link.start()
        await link.transfer({
            "entities": [{"name": "EEEV"}, {"name": "VEEV"}],
            "query_terms": ["EEEV", "VEEV"],
        })
        await link.stop()

    asyncio.run(run())
    assert target.received == [{"query_terms": ["EEEV", "VEEV"]}]


def test_transform_link_llm_proposals_to_approved_mappings_end_to_end():
    link = _make_link(
        "apecx_integration.composition.transforms.llm_proposals_to_approved_mappings"
    )
    target = _FakeTarget()
    link.target = target

    async def run():
        await link.start()
        await link.transfer({
            "llm_proposals": [
                {"query_entity": "EEEV", "synonym": "EEEV stub", "score": 0.9},
            ],
        })
        await link.stop()

    asyncio.run(run())
    assert len(target.received) == 1
    assert target.received[0]["approved_mappings"][0]["query_term"] == "EEEV"
