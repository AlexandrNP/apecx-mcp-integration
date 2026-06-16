"""Unit tests for HarmonizedBundleMergeStep.

The step fans the per-index ``HarmonizedSearchExecuteStep`` result envelopes in
(the ``items`` list produced by the per-index map) into a single
``globus_results`` list, and derives ``taxon_id`` + ``resolved_species_name``
from the ``resolution_plan``. The unit tests construct fake per-index envelopes
matching the real ``HarmonizedSearchExecuteStep`` output shape.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from apecx_integration.composition.steps.harmonized_bundle_merge_step import (
    HarmonizedBundleMergeStep,
)
from apecx_integration.composition.steps.harmonized_search_execute_step import (
    _INDEX_UUIDS,
)


def _stage(tmp_path: Path) -> HarmonizedBundleMergeStep:
    p = tmp_path / "harmonized_bundle_merge.yml"
    p.write_text("name: harmonized_bundle_merge_test\n")
    return HarmonizedBundleMergeStep.from_config(str(p))


def _ok_item(index: str, harm_count: int, raw_count: int = 0) -> dict:
    """Build a per-index HarmonizedSearchExecuteStep OK envelope with N harmonized
    + M raw sample records (the real shape: records ride under
    ``envelope_input.data.parts.{harmonized_query,raw_query}.sample``)."""
    harm_sample = [
        {"title": f"{index} harm {i}", "identifier": f"{index}:H{i}"} for i in range(harm_count)
    ]
    raw_sample = [
        {"title": f"{index} raw {i}", "identifier": f"{index}:R{i}"} for i in range(raw_count)
    ]
    return {
        "envelope_input": {
            "markdown": f"### Harmonized search on `{index}`",
            "data": {
                "kind": "bundle",
                "parts": {
                    "resolution": {"path": "fast"},
                    "raw_query": {"total": raw_count, "sample": raw_sample},
                    "harmonized_query": {"total": harm_count, "sample": harm_sample},
                    "status": "ok",
                },
            },
        }
    }


def _paused_item(index: str) -> dict:
    """A paused (ambiguous) envelope carries no record sample → contributes 0."""
    return {
        "envelope_input": {
            "markdown": f"### ambiguous on `{index}`",
            "data": {
                "kind": "bundle",
                "parts": {
                    "resolution": {"path": "ambiguous"},
                    "status": "paused_awaiting_disambiguation",
                },
            },
        }
    }


def test_loads_via_from_config(tmp_path):
    step = _stage(tmp_path)
    assert step.name == "harmonized_bundle_merge_test"


def test_nine_items_flatten_and_derive_taxon(tmp_path):
    index_names = sorted(_INDEX_UUIDS)
    assert len(index_names) == 9
    # 9 items: each contributes 2 harmonized records.
    items = [_ok_item(name, harm_count=2) for name in index_names]
    bundle = {
        "query": "chikungunya epitopes",
        "protein": "E2",
        "items": items,
        "index_names": index_names,
        "_map_errors": [],
        "resolution_plan": {
            "canonical_iri": "http://purl.obolibrary.org/obo/NCBITaxon_37124",
            "canonical_label": "Chikungunya virus",
        },
    }
    step = _stage(tmp_path)
    out = asyncio.run(step.process(bundle))

    assert len(out["globus_results"]) == 18  # 9 indices x 2 records
    assert out["taxon_id"] == 37124
    assert out["resolved_species_name"] == "Chikungunya virus"

    summary = out["harmonized_search_summary"]
    assert summary["total_records"] == 18
    assert summary["per_index_kept"][index_names[0]] == 2
    assert len(summary["per_index_kept"]) == 9
    assert summary["map_errors"] == []

    # passthrough preserved
    assert out["query"] == "chikungunya epitopes"
    assert out["protein"] == "E2"


def test_mixed_items_skip_empty(tmp_path):
    index_names = sorted(_INDEX_UUIDS)
    items = [
        _ok_item(index_names[0], harm_count=3, raw_count=1),  # 4 records
        _paused_item(index_names[1]),  # 0
        _ok_item(index_names[2], harm_count=0, raw_count=0),  # 0
    ]
    bundle = {
        "query": "rsv",
        "items": items,
        "index_names": index_names[:3],
        "resolution_plan": {
            "canonical_iri": "http://purl.obolibrary.org/obo/NCBITaxon_37124",
            "canonical_label": "Chikungunya virus",
        },
    }
    step = _stage(tmp_path)
    out = asyncio.run(step.process(bundle))
    assert len(out["globus_results"]) == 4
    assert out["harmonized_search_summary"]["per_index_kept"][index_names[1]] == 0
    assert out["harmonized_search_summary"]["per_index_kept"][index_names[2]] == 0


def test_empty_items_degrade_loud(tmp_path):
    """No items / no plan → empty globus_results + None taxon, never a raise."""
    bundle = {"query": "chikungunya", "items": []}
    step = _stage(tmp_path)
    out = asyncio.run(step.process(bundle))
    assert out["globus_results"] == []
    assert out["taxon_id"] is None
    assert out["resolved_species_name"] is None
    assert out["harmonized_search_summary"]["total_records"] == 0


def test_missing_items_key_degrade_loud(tmp_path):
    bundle = {"query": "chikungunya"}
    step = _stage(tmp_path)
    out = asyncio.run(step.process(bundle))
    assert out["globus_results"] == []
    assert out["taxon_id"] is None


def test_plan_without_iri_yields_none_taxon(tmp_path):
    bundle = {
        "query": "obscure",
        "items": [_ok_item(sorted(_INDEX_UUIDS)[0], harm_count=1)],
        "index_names": sorted(_INDEX_UUIDS),
        "resolution_plan": {"canonical_iri": None, "canonical_label": None},
    }
    step = _stage(tmp_path)
    out = asyncio.run(step.process(bundle))
    assert len(out["globus_results"]) == 1
    assert out["taxon_id"] is None
    assert out["resolved_species_name"] is None


def test_unwraps_trigger_envelope(tmp_path):
    index_names = sorted(_INDEX_UUIDS)
    inner = {
        "query": "chikungunya",
        "items": [_ok_item(index_names[0], harm_count=2)],
        "index_names": index_names,
        "resolution_plan": {
            "canonical_iri": "http://purl.obolibrary.org/obo/NCBITaxon_37124",
            "canonical_label": "Chikungunya virus",
        },
    }
    step = _stage(tmp_path)
    out = asyncio.run(step.process({"hmerge_input": inner}))
    assert len(out["globus_results"]) == 2
    assert out["taxon_id"] == 37124
    assert out["query"] == "chikungunya"


def test_non_dict_raises(tmp_path):
    step = _stage(tmp_path)
    with pytest.raises(ValueError, match="must be a dict"):
        asyncio.run(step.process("not a dict"))
