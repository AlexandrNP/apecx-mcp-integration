"""Probe batch 44 — adversarial probes against the SynonymCacheLookupStep
and VerifiedSynonymWritebackStep surfaces.

Streak before this batch: 99/300 post-AQ post-1066.
Probe naming: 1155–1179.

Distinct probes only.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from apecx_integration.composition.steps.synonym_cache import (
    SynonymCacheLookupStep,
    SynonymCacheLookupStepConfig,
    VerifiedSynonymWritebackStep,
    VerifiedSynonymWritebackStepConfig,
)

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = (
    REPO_ROOT / "src" / "apecx_integration" / "composition" / "workflows" / "violin_bvbrc"
)
SCL_YAML = WORKFLOW_DIR / "steps" / "synonym_cache_lookup.yml"
WRITEBACK_YAML = WORKFLOW_DIR / "steps" / "verified_synonym_writeback.yml"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class _MockResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        # G38-WA-1 closure (2026-05-11): HTTPBackendAdapter inspects
        # ``status_code`` (httpx-canonical) + ``headers`` + ``text``,
        # not ``status`` + ``json()`` only. Expose the canonical
        # surface so the mock plays both with the legacy direct-httpx
        # callsite (``status`` + ``raise_for_status``) AND with the
        # adapter (``status_code`` + ``headers`` + ``text`` + ``json``).
        self.status_code = status

    @property
    def headers(self) -> dict[str, str]:
        return {"content-type": "application/json"}

    @property
    def text(self) -> str:
        import json as _json

        return _json.dumps(self.payload)

    def json(self) -> dict:
        return self.payload

    def raise_for_status(self) -> None:
        if self.status >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                "mock",
                request=None,
                response=None,
            )


class _MockClient:
    """Captures POST calls; returns canned response per path."""

    def __init__(self, responses: dict[str, _MockResponse]) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        pass

    async def post(self, path: str, json: dict | None = None, **kwargs):
        # G38-WA-1 closure (2026-05-11): the step now dispatches via
        # HTTPBackendAdapter, which passes ``timeout`` + ``headers``
        # kwargs through to the underlying client's ``.post()``. Mock
        # accepts arbitrary keyword args via ``**kwargs`` so the
        # adapter-mediated call shape is honored without each test
        # needing to know the adapter's internals. The captured call
        # dict still only carries path + json — kwargs are noise.
        self.calls.append({"path": path, "json": json})
        return self.responses[path]


def _patch_client(step, mock_client):
    step._http_client_factory = lambda: mock_client


# --------------------------------------------------------------------------- #
# Probes 1155–1179
# --------------------------------------------------------------------------- #


def test_probe_1155_synonym_cache_step_loads_via_from_config():
    step = SynonymCacheLookupStep.from_config(str(SCL_YAML))
    assert step.name == "synonym_cache_lookup"


def test_probe_1156_synonym_cache_step_rejects_non_list_query_terms():
    step = SynonymCacheLookupStep.from_config(str(SCL_YAML))
    with pytest.raises(ValueError, match="'query_terms' as list"):
        asyncio.run(step.process({"query_terms": "not a list"}))


def test_probe_1157_synonym_cache_step_rejects_list_with_non_str_elements():
    step = SynonymCacheLookupStep.from_config(str(SCL_YAML))
    with pytest.raises(ValueError, match="'query_terms' as list"):
        asyncio.run(step.process({"query_terms": ["ok", 42]}))


def test_probe_1158_synonym_cache_step_empty_query_terms_is_noop():
    """Empty list is a no-op (not error). Idempotency under empty
    upstream entity-extraction is the contract."""
    step = SynonymCacheLookupStep.from_config(str(SCL_YAML))
    out = asyncio.run(step.process({"query_terms": []}))
    assert out == {"cached_mappings": {}, "novel_terms": []}


def test_probe_1159_synonym_cache_step_handles_missing_matches_field():
    """A control-plane response without the 'matches' field must
    raise loudly — silent fallback would propagate empty cache.

    The step's contract requires this; verify."""
    step = SynonymCacheLookupStep.from_config(str(SCL_YAML))
    mock = _MockClient(
        {
            "/verified_synonyms/lookup": _MockResponse(
                {"weird_shape": []}  # no 'matches'
            ),
        }
    )
    _patch_client(step, mock)
    with pytest.raises(ValueError, match="'matches' list"):
        asyncio.run(step.process({"query_terms": ["eeev"]}))


def test_probe_1160_synonym_cache_step_handles_matches_as_non_list():
    """matches=str (sloppy CP response) must reject."""
    step = SynonymCacheLookupStep.from_config(str(SCL_YAML))
    mock = _MockClient(
        {
            "/verified_synonyms/lookup": _MockResponse({"matches": "not a list"}),
        }
    )
    _patch_client(step, mock)
    with pytest.raises(ValueError, match="'matches' list"):
        asyncio.run(step.process({"query_terms": ["eeev"]}))


def test_probe_1161_synonym_cache_step_partitions_cached_vs_novel():
    """A response with mixed result/result=None matches partitions
    correctly into cached_mappings + novel_terms."""
    step = SynonymCacheLookupStep.from_config(str(SCL_YAML))
    mock = _MockClient(
        {
            "/verified_synonyms/lookup": _MockResponse(
                {
                    "matches": [
                        {"query_term": "eeev", "result": {"canonical_term": "VIOLIN_205"}},
                        {"query_term": "novel_term", "result": None},
                    ]
                }
            ),
        }
    )
    _patch_client(step, mock)
    out = asyncio.run(step.process({"query_terms": ["eeev", "novel_term"]}))
    assert out["cached_mappings"] == {"eeev": "VIOLIN_205"}
    assert out["novel_terms"] == ["novel_term"]


def test_probe_1162_synonym_cache_step_payload_includes_source_target_scope():
    """The POST body must carry source_vocabulary / target_vocabulary /
    scope from the step's config. Pin: the scope is configurable."""
    step = SynonymCacheLookupStep.from_config(str(SCL_YAML))
    mock = _MockClient(
        {
            "/verified_synonyms/lookup": _MockResponse({"matches": []}),
        }
    )
    _patch_client(step, mock)
    asyncio.run(step.process({"query_terms": ["x"]}))
    call = mock.calls[0]
    assert "source_vocabulary" in call["json"]
    assert "target_vocabulary" in call["json"]
    assert "scope" in call["json"]
    assert call["json"]["query_terms"] == ["x"]


def test_probe_1163_synonym_cache_step_does_not_mutate_input(caplog):
    step = SynonymCacheLookupStep.from_config(str(SCL_YAML))
    mock = _MockClient(
        {
            "/verified_synonyms/lookup": _MockResponse({"matches": []}),
        }
    )
    _patch_client(step, mock)
    inputs = {"query_terms": ["a", "b", "c"]}
    snapshot = list(inputs["query_terms"])
    asyncio.run(step.process(inputs))
    assert inputs["query_terms"] == snapshot


def test_probe_1164_synonym_cache_step_class_attributes():
    assert SynonymCacheLookupStep.COMPONENT_TYPE
    assert "name" in SynonymCacheLookupStep.REQUIRED_CONFIG_FIELDS


def test_probe_1165_synonym_cache_step_process_is_coroutine():
    assert inspect.iscoroutinefunction(SynonymCacheLookupStep.process)


def test_probe_1166_writeback_step_loads_via_from_config():
    step = VerifiedSynonymWritebackStep.from_config(str(WRITEBACK_YAML))
    assert step.name == "verified_synonym_writeback"


def test_probe_1167_writeback_step_class_attributes():
    assert VerifiedSynonymWritebackStep.COMPONENT_TYPE
    assert "name" in VerifiedSynonymWritebackStep.REQUIRED_CONFIG_FIELDS


def test_probe_1168_writeback_step_process_is_coroutine():
    assert inspect.iscoroutinefunction(VerifiedSynonymWritebackStep.process)


def test_probe_1169_synonym_cache_config_has_required_fields():
    """Verify SynonymCacheLookupStepConfig declares the expected fields:
    source_vocabulary, target_vocabulary."""
    fields = SynonymCacheLookupStepConfig.model_fields
    assert "source_vocabulary" in fields
    assert "target_vocabulary" in fields


def test_probe_1170_writeback_config_extends_step_config():
    """The Writeback config must extend StepConfig (so it inherits
    common fields like name, description, executor_config)."""
    from nanobrain.core.step import StepConfig

    assert issubclass(VerifiedSynonymWritebackStepConfig, StepConfig)


def test_probe_1171_synonym_cache_step_yaml_input_data_unit_is_query_terms_input():
    """Pin the wrapper YAML's input data unit name. A rename here
    would silently break the link from upstream entity_extraction."""
    import yaml

    raw = yaml.safe_load(SCL_YAML.read_text())
    inputs = raw.get("input_data_units", {})
    assert "query_terms_input" in inputs


def test_probe_1172_writeback_step_yaml_input_data_unit_is_approved_mappings_input():
    """Pin the wrapper YAML's input data unit name."""
    import yaml

    raw = yaml.safe_load(WRITEBACK_YAML.read_text())
    inputs = raw.get("input_data_units", {})
    assert "approved_mappings_input" in inputs


def test_probe_1173_synonym_cache_step_yaml_output_data_unit_is_cache_lookup_output():
    import yaml

    raw = yaml.safe_load(SCL_YAML.read_text())
    outputs = raw.get("output_data_units", {})
    assert "cache_lookup_output" in outputs


def test_probe_1174_writeback_step_yaml_output_data_unit_is_writeback_output():
    import yaml

    raw = yaml.safe_load(WRITEBACK_YAML.read_text())
    outputs = raw.get("output_data_units", {})
    assert "writeback_output" in outputs


def test_probe_1175_synonym_cache_yaml_cp_url_is_env_overridable():
    """The control plane URL must be configurable per-deployment.
    It's OK to have ``http://localhost:8000`` as a literal default
    INSIDE ``${CONTROL_PLANE_URL:-...}`` env-var-override syntax —
    that's correctly configurable. Pin the env-var override
    pattern so a future commit accidentally dropping the
    ``${CONTROL_PLANE_URL:-...}`` wrapper (leaving a bare
    localhost) is caught."""
    text = SCL_YAML.read_text()
    # If localhost appears at all, it must be inside the env-var
    # default-fallback syntax.
    if "localhost" in text:
        assert "${CONTROL_PLANE_URL" in text, (
            f"localhost reference in {SCL_YAML.name} but no "
            f"${{CONTROL_PLANE_URL:-...}} env-var-override "
            f"wrapper -- the URL would be hardcoded across "
            f"deployments"
        )


def test_probe_1176_workflow_yaml_links_synonym_chain_correctly():
    """The T01 chain link block must wire entity_extraction to
    synonym_cache_lookup correctly. Pin to catch a future
    accidental rewrite."""
    import yaml

    raw = yaml.safe_load((WORKFLOW_DIR / "violin_bvbrc_workflow.yml").read_text())
    links = raw["links"]
    sc_link = links["step1_to_step3a"]["config"]
    assert sc_link["source"] == "entity_extraction.entity_candidates_output"
    assert sc_link["target"] == "synonym_cache_lookup.query_terms_input"


def test_probe_1177_workflow_yaml_links_writeback_chain_correctly():
    import yaml

    raw = yaml.safe_load((WORKFLOW_DIR / "violin_bvbrc_workflow.yml").read_text())
    links = raw["links"]
    wb_link = links["step4p_to_step7"]["config"]
    assert wb_link["source"] == "verified_synonym_writeback.writeback_output"
    assert wb_link["target"] == "result_ranking.enriched_results_input"


def test_probe_1178_synonym_cache_step_yaml_has_DataUnitChangeTrigger():
    """The trigger must be DataUnitChangeTrigger on query_terms_input.
    A different trigger type would silently break the workflow's
    event flow."""
    import yaml

    raw = yaml.safe_load(SCL_YAML.read_text())
    triggers = raw.get("triggers", [])
    assert any(
        t.get("class", "").endswith("DataUnitChangeTrigger")
        and t.get("data_unit") == "query_terms_input"
        for t in triggers
    )


def test_probe_1179_writeback_step_yaml_has_DataUnitChangeTrigger():
    import yaml

    raw = yaml.safe_load(WRITEBACK_YAML.read_text())
    triggers = raw.get("triggers", [])
    assert any(
        t.get("class", "").endswith("DataUnitChangeTrigger")
        and t.get("data_unit") == "approved_mappings_input"
        for t in triggers
    )
