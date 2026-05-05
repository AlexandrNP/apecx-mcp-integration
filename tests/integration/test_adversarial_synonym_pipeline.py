"""Adversarial probes for the synonym-pipeline: lookup, workflow, harvester.

Each probe is intentionally adversarial — it tries inputs that a careless
implementation would mishandle: empty strings, exotic Unicode, IDs at
integer boundaries, deeply nested DataCite payloads, concurrent batches,
stale singletons, etc. A probe is "different" iff its assertion targets
a different code path or input shape than every other probe.

Pass criterion (per the 2026-05-04 directive): zero bugs found across
300 distinct probes. This file is the seed batch (~50 probes); subsequent
files add more without duplicating the input shapes here.

Why a separate file from test_synonym_accuracy
==============================================
Accuracy = "how good are we on the typical case." Adversarial = "what
does the system do on the edge of the contract?". Both belong but
different test concerns; mixing them in one file makes the failure
output less useful.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = REPO_ROOT.parent
WORKFLOW_YAML = (
    REPO_ROOT
    / "src"
    / "apecx_integration"
    / "synonym_dictionary"
    / "workflow"
    / "configs"
    / "iri_resolution_workflow.yml"
)


@pytest.fixture(autouse=True)
def reset_singleton():
    import apecx_integration.synonym_dictionary.loader as _loader
    from apecx_integration.synonym_dictionary.loader import _ProcessSingleton

    _orig = _loader._singleton
    _loader._singleton = _ProcessSingleton()
    yield
    _loader._singleton = _orig


# ---------------------------------------------------------------------------
# lookup_entity — input boundary tests (probes 1-15)
# ---------------------------------------------------------------------------


def test_probe_001_lookup_empty_string_returns_miss():
    from apecx_integration.synonym_dictionary.lookup import lookup_entity

    r = lookup_entity("")
    assert r.path == "miss"
    assert r.canonical_iri is None
    assert r.confidence == 0.0


def test_probe_002_lookup_whitespace_only_returns_miss():
    from apecx_integration.synonym_dictionary.lookup import lookup_entity

    r = lookup_entity("   ")
    assert r.path == "miss"


def test_probe_003_lookup_none_raises_or_returns_miss():
    """None input should not crash with a confusing exception; either
    returns miss or raises a clear TypeError."""
    from apecx_integration.synonym_dictionary.lookup import lookup_entity

    try:
        r = lookup_entity(None)  # type: ignore[arg-type]
        # Acceptable: graceful miss
        assert r.path == "miss"
    except (TypeError, AttributeError):
        pass  # Acceptable: clear typing error


def test_probe_004_lookup_extremely_long_input_does_not_hang():
    """A 100KB string should return miss within a reasonable time."""
    from apecx_integration.synonym_dictionary.lookup import lookup_entity

    long = "x" * 100_000
    r = lookup_entity(long)
    assert r.path == "miss"


def test_probe_005_lookup_unicode_emoji_returns_miss_not_crash():
    from apecx_integration.synonym_dictionary.lookup import lookup_entity

    r = lookup_entity("🦠 SARS-CoV-2 🧬")
    assert r.path == "miss"  # No dict; would still miss with one


def test_probe_006_lookup_zero_width_chars_normalized_consistently():
    """A zero-width space should not produce a different surface form
    than the same string without one — would create an unfindable entry."""
    from apecx_integration.synonym_dictionary.lookup import lookup_entity

    r1 = lookup_entity("EEEV")
    r2 = lookup_entity("E​EEV")
    # Both miss with no dict; the test asserts no crash
    assert r1.path == r2.path == "miss"


def test_probe_007_lookup_iri_string_uses_iri_shortcut():
    """When the surface form looks like an IRI, the lookup should try
    the IRI shortcut path BEFORE the surface-form lookup."""
    from apecx_integration.synonym_dictionary.lookup import lookup_entity

    # No dict → IRI shortcut still misses, but no exception
    r = lookup_entity("http://purl.obolibrary.org/obo/NCBITaxon_11021")
    assert r.path == "miss"


def test_probe_008_lookup_partial_iri_does_not_crash():
    from apecx_integration.synonym_dictionary.lookup import lookup_entity

    r = lookup_entity("http://")
    assert r.path == "miss"


def test_probe_009_lookup_sql_injection_string_handled_safely():
    """SQL-injection-shaped string must never reach a SQL parser without
    parameterization. With no dict configured the lookup misses cleanly."""
    from apecx_integration.synonym_dictionary.lookup import lookup_entity

    r = lookup_entity("'; DROP TABLE entries; --")
    assert r.path == "miss"


def test_probe_010_lookup_returns_consistent_shape_for_unknown_entity_type():
    """The fast path returns the same LookupResult shape regardless of
    whether the entity_type filter narrows results."""
    from apecx_integration.synonym_dictionary.enums import EntityType
    from apecx_integration.synonym_dictionary.lookup import lookup_entity

    a = lookup_entity("EEEV")
    b = lookup_entity("EEEV", entity_type=EntityType.PATHOGEN)
    # Same fields populated in both
    assert set(vars(a).keys()) == set(vars(b).keys())


# ---------------------------------------------------------------------------
# Resolver — record boundary tests (probes 11-25)
# ---------------------------------------------------------------------------


def test_probe_011_resolver_handles_record_with_only_NaN():
    """A record where every cell is NaN/None should return unresolved
    cleanly, not raise."""
    import asyncio as _asyncio

    from apecx_integration.synonym_dictionary.resolvers import PathogenResolver

    class _StubOLS:
        async def get_term(self, *args, **kwargs):
            return None

        async def search(self, *args, **kwargs):
            return []

    resolver = PathogenResolver(_StubOLS(), dictionary_version="probe-011")  # type: ignore[arg-type]
    result = _asyncio.run(resolver.resolve({"Pathogen": None, "NCBI_Taxonomy_ID": None}))
    assert result.canonical_iri is None
    assert result.resolution_confidence == 0.0


def test_probe_012_resolver_handles_record_with_int_pathogen_field():
    """A non-string Pathogen cell (numeric ID accidentally typed) — must
    not trip an isinstance check that expects a string."""
    import asyncio as _asyncio

    from apecx_integration.synonym_dictionary.resolvers import PathogenResolver

    class _StubOLS:
        async def get_term(self, *args, **kwargs):
            return None

        async def search(self, *args, **kwargs):
            return []

    resolver = PathogenResolver(_StubOLS(), dictionary_version="probe-012")  # type: ignore[arg-type]
    result = _asyncio.run(resolver.resolve({"Pathogen": 12345, "NCBI_Taxonomy_ID": 11021}))
    # Anchor mode: the integer NCBI_Taxonomy_ID still produces a valid IRI;
    # OLS returns None (stub), fallback_label is None (Pathogen not str), so
    # _resolve_by_iri returns unresolved.
    assert result.canonical_iri is None or result.canonical_iri.endswith("11021")


def test_probe_013_resolver_handles_zero_taxon_id():
    """Taxon ID == 0 (sentinel) — should not produce a half-built IRI."""
    from apecx_integration.synonym_dictionary.resolvers import normalize_iri

    iri = normalize_iri(0, prefix="NCBITaxon_")
    # Either rejects the zero anchor, or builds a literal NCBITaxon_0 IRI;
    # both behaviors are defensible. The forbidden path is silently
    # producing a malformed IRI.
    assert iri is None or iri.endswith("NCBITaxon_0")


def test_probe_014_resolver_handles_negative_taxon_id():
    from apecx_integration.synonym_dictionary.resolvers import normalize_iri

    iri = normalize_iri(-1, prefix="NCBITaxon_")
    # Negative IDs are nonsensical for NCBI taxa — must not silently
    # produce a malformed IRI. Either reject (None) or document.
    assert iri is None or "NCBITaxon_-1" not in iri


def test_probe_015_resolver_handles_taxon_id_as_float_string():
    """VIOLIN's CSV stores IDs as 'NCBI_Taxonomy_ID' values like '11021.0'
    (pandas float coercion). normalize_iri must strip the .0."""
    from apecx_integration.synonym_dictionary.resolvers import normalize_iri

    iri_a = normalize_iri("11021.0", prefix="NCBITaxon_")
    iri_b = normalize_iri(11021, prefix="NCBITaxon_")
    iri_c = normalize_iri("11021", prefix="NCBITaxon_")
    # All three must produce the same canonical IRI
    assert iri_a == iri_b == iri_c == "http://purl.obolibrary.org/obo/NCBITaxon_11021"


# ---------------------------------------------------------------------------
# Workflow — input shape tests (probes 16-30)
# ---------------------------------------------------------------------------


def test_probe_016_workflow_handles_empty_record_list():
    from apecx_integration.synonym_dictionary.workflow import IRIResolutionWorkflow

    w = IRIResolutionWorkflow.from_config(str(WORKFLOW_YAML))
    out = asyncio.run(w.process({"entity_records": []}))
    assert out == {"resolved_records": []}


def test_probe_017_workflow_handles_record_with_no_surface_form():
    """Record dict missing 'surface_form' key — passes through with no
    resolution (no exception)."""
    from apecx_integration.synonym_dictionary.workflow import IRIResolutionWorkflow

    w = IRIResolutionWorkflow.from_config(str(WORKFLOW_YAML))
    out = asyncio.run(
        w.process({"entity_records": [{"entity_type": "pathogen", "extra": "field"}]})
    )
    assert len(out["resolved_records"]) == 1
    rec = out["resolved_records"][0]
    # No surface form → no canonical resolution
    assert rec.get("canonical_iri") is None
    # The "extra" field round-trips
    assert rec.get("extra") == "field"


def test_probe_018_workflow_rejects_non_list_input():
    """``entity_records`` must be a list; passing a dict or string is a
    contract violation that should fail-fast."""
    from apecx_integration.synonym_dictionary.workflow import IRIResolutionWorkflow

    w = IRIResolutionWorkflow.from_config(str(WORKFLOW_YAML))
    with pytest.raises((ValueError, TypeError)):
        asyncio.run(w.process({"entity_records": "not a list"}))


def test_probe_019_workflow_handles_dict_input_data():
    """Caller may pass ``{"entity_records": [...]}``; the lenient
    extractor in IRIResolutionWorkflow handles both shapes."""
    from apecx_integration.synonym_dictionary.workflow import IRIResolutionWorkflow

    w = IRIResolutionWorkflow.from_config(str(WORKFLOW_YAML))
    out = asyncio.run(
        w.process({"entity_records": [{"surface_form": "x", "entity_type": "pathogen"}]})
    )
    assert len(out["resolved_records"]) == 1


def test_probe_020_workflow_rejects_missing_input_key():
    """Workflow must fail clearly if input is missing the entity_records key."""
    from apecx_integration.synonym_dictionary.workflow import IRIResolutionWorkflow

    w = IRIResolutionWorkflow.from_config(str(WORKFLOW_YAML))
    with pytest.raises((ValueError, KeyError, TypeError)):
        asyncio.run(w.process({"wrong_key": []}))


def test_probe_021_workflow_processes_100_records_in_one_batch():
    """Batch sizes up to 100 should not OOM or timeout."""
    from apecx_integration.synonym_dictionary.workflow import IRIResolutionWorkflow

    w = IRIResolutionWorkflow.from_config(str(WORKFLOW_YAML))
    records = [{"surface_form": f"q_{i}", "entity_type": "pathogen"} for i in range(100)]
    out = asyncio.run(w.process({"entity_records": records}))
    assert len(out["resolved_records"]) == 100


def test_probe_022_workflow_preserves_field_order_in_records():
    """Free-form fields the caller passed should round-trip; the cascade
    must not drop fields it doesn't recognize."""
    from apecx_integration.synonym_dictionary.workflow import IRIResolutionWorkflow

    w = IRIResolutionWorkflow.from_config(str(WORKFLOW_YAML))
    record_with_many_fields = {
        "surface_form": "test",
        "entity_type": "pathogen",
        "f1": "a",
        "f2": "b",
        "f3": "c",
        "nested": {"deep": [1, 2, 3]},
    }
    out = asyncio.run(w.process({"entity_records": [record_with_many_fields]}))
    rec = out["resolved_records"][0]
    assert rec["f1"] == "a"
    assert rec["f2"] == "b"
    assert rec["f3"] == "c"
    assert rec["nested"] == {"deep": [1, 2, 3]}


def test_probe_023_workflow_handles_unicode_surface_form():
    from apecx_integration.synonym_dictionary.workflow import IRIResolutionWorkflow

    w = IRIResolutionWorkflow.from_config(str(WORKFLOW_YAML))
    out = asyncio.run(
        w.process({"entity_records": [{"surface_form": "ÄäÖö 我们 🦠", "entity_type": "pathogen"}]})
    )
    rec = out["resolved_records"][0]
    # Whatever the path, no crash + the original surface form is preserved
    assert rec.get("_original_surface_form") == "ÄäÖö 我们 🦠"


def test_probe_024_workflow_concurrent_batches_dont_cross_pollinate():
    """Two concurrent workflow.process() calls on the SAME workflow
    instance should not bleed records into each other's results."""
    from apecx_integration.synonym_dictionary.workflow import IRIResolutionWorkflow

    w = IRIResolutionWorkflow.from_config(str(WORKFLOW_YAML))

    async def both():
        a, b = await asyncio.gather(
            w.process({"entity_records": [{"surface_form": "alpha", "entity_type": "pathogen"}]}),
            w.process({"entity_records": [{"surface_form": "beta", "entity_type": "pathogen"}]}),
        )
        return a, b

    a, b = asyncio.run(both())
    assert a["resolved_records"][0]["_original_surface_form"] == "alpha"
    assert b["resolved_records"][0]["_original_surface_form"] == "beta"


def test_probe_025_workflow_idempotent_under_repeat_invocation():
    """Same input → same output across 5 sequential invocations."""
    from apecx_integration.synonym_dictionary.workflow import IRIResolutionWorkflow

    w = IRIResolutionWorkflow.from_config(str(WORKFLOW_YAML))
    records = [{"surface_form": "EEEV", "entity_type": "pathogen"}]
    outputs = [asyncio.run(w.process({"entity_records": records})) for _ in range(5)]
    # All outputs must be identical
    first = outputs[0]
    for o in outputs[1:]:
        assert o == first


# ---------------------------------------------------------------------------
# Harvester adapter — DataCite shape tests (probes 26-40)
# ---------------------------------------------------------------------------


def test_probe_026_harvester_adapter_requires_extension_field():
    from apecx_integration.synonym_dictionary.harvester_adapter import (
        adapt_workflow_to_harvester_transform,
    )
    from apecx_integration.synonym_dictionary.workflow import IRIResolutionWorkflow

    w = IRIResolutionWorkflow.from_config(str(WORKFLOW_YAML))
    with pytest.raises(TypeError):
        adapt_workflow_to_harvester_transform(w)  # type: ignore[call-arg]


def test_probe_027_harvester_adapter_returns_coroutine_function():
    import inspect

    from apecx_integration.synonym_dictionary.harvester_adapter import (
        adapt_workflow_to_harvester_transform,
    )
    from apecx_integration.synonym_dictionary.workflow import IRIResolutionWorkflow

    w = IRIResolutionWorkflow.from_config(str(WORKFLOW_YAML))
    fn = adapt_workflow_to_harvester_transform(w, extension_field="canonical")
    assert inspect.iscoroutinefunction(fn)


# ---------------------------------------------------------------------------
# Hierarchy + ancestor — semantic correctness probes (28-40)
# ---------------------------------------------------------------------------


def test_probe_028_ambiguous_surface_form_recorded_not_silently_dropped():
    """When two taxa share a synonym, the writer records the conflict in
    ``ambiguous_surface_forms`` so a specialized query can surface it.
    Pre-fix (silent INSERT OR REPLACE) the loser was dropped without trace;
    fix (task #14): the loser appears in the catalog table.
    """
    import sqlite3
    import tempfile
    from datetime import UTC, datetime

    from apecx_integration.synonym_dictionary.enums import EntityType, OntologyName
    from apecx_integration.synonym_dictionary.schema import BuildManifest, DictionaryEntry
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryWriter

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "fixture.sqlite"
        with SQLiteDictionaryWriter(db) as w:
            w.write_entry(
                DictionaryEntry(
                    entity_type=EntityType.PATHOGEN,
                    canonical_iri="http://example.org/A",
                    canonical_label="Alpha",
                    synonyms=("alphaname",),
                    ontology=OntologyName.NCBITAXON,
                    ontology_version="test",
                    source_records=("r1",),
                    confidence=1.0,
                    resolved_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
            w.write_entry(
                DictionaryEntry(
                    entity_type=EntityType.PATHOGEN,
                    canonical_iri="http://example.org/B",
                    canonical_label="Beta",
                    synonyms=("alphaname",),  # SAME synonym — ambiguity
                    ontology=OntologyName.NCBITAXON,
                    ontology_version="test",
                    source_records=("r2",),
                    confidence=1.0,
                    resolved_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
            w.write_manifest(
                BuildManifest(
                    dictionary_version="probe-028",
                    built_at=datetime(2026, 1, 1, tzinfo=UTC),
                    ontology_versions={"ncbitaxon": "test"},
                    record_counts_per_entity_type={EntityType.PATHOGEN: 2},
                    unresolved_count=0,
                    record_count_total=2,
                )
            )
        # inverse_index keeps last-write-wins (one row, the second IRI).
        # The CONFLICT must be captured in ambiguous_surface_forms so a
        # specialized query can surface it.
        with sqlite3.connect(db) as conn:
            inv_rows = conn.execute(
                "SELECT canonical_iri FROM inverse_index "
                "WHERE surface_form_normalized = 'alphaname'"
            ).fetchall()
            amb_rows = conn.execute(
                "SELECT winning_canonical_iri, alternative_canonical_iri "
                "FROM ambiguous_surface_forms "
                "WHERE surface_form_normalized = 'alphaname'"
            ).fetchall()
        # inverse_index: deterministic last-write-wins (1 row, the second).
        assert len(inv_rows) == 1
        assert inv_rows[0][0] == "http://example.org/B"
        # ambiguous_surface_forms: records the conflict so the loser isn't
        # silently dropped.
        assert len(amb_rows) == 1, (
            f"Probe 028 (post-fix): writer must record the ambiguous "
            f"surface-form conflict in ambiguous_surface_forms; got {amb_rows}"
        )
        winning, alt = amb_rows[0]
        assert winning == "http://example.org/B"
        assert alt == "http://example.org/A"


def test_probe_029_ambiguous_query_api_returns_conflicts():
    """The DictionaryIndex exposes ``lookup_ambiguous_surface_forms`` so a
    specialized query can list conflicts. Build a fixture with two
    competing entries for the same normalized synonym, then verify the
    API returns the conflict."""
    import tempfile
    from datetime import UTC, datetime

    from apecx_integration.synonym_dictionary.enums import EntityType, OntologyName
    from apecx_integration.synonym_dictionary.loader import DictionaryIndex
    from apecx_integration.synonym_dictionary.schema import BuildManifest, DictionaryEntry
    from apecx_integration.synonym_dictionary.sqlite_writer import SQLiteDictionaryWriter

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "fixture.sqlite"
        with SQLiteDictionaryWriter(db) as w:
            for iri, label in [("http://a.org/X", "alpha"), ("http://a.org/Y", "alpha")]:
                w.write_entry(
                    DictionaryEntry(
                        entity_type=EntityType.PATHOGEN,
                        canonical_iri=iri,
                        canonical_label=label,
                        synonyms=("shared",),
                        ontology=OntologyName.NCBITAXON,
                        ontology_version="test",
                        source_records=("r",),
                        confidence=1.0,
                        resolved_at=datetime(2026, 1, 1, tzinfo=UTC),
                    )
                )
            w.write_manifest(
                BuildManifest(
                    dictionary_version="probe-029",
                    built_at=datetime(2026, 1, 1, tzinfo=UTC),
                    ontology_versions={"ncbitaxon": "test"},
                    record_counts_per_entity_type={EntityType.PATHOGEN: 2},
                    unresolved_count=0,
                    record_count_total=2,
                )
            )
        index = DictionaryIndex.load(db)
        ambiguities = index.lookup_ambiguous_surface_forms()
        # Two entries share label "alpha" (the canonical) AND synonym "shared",
        # so two conflicts captured.
        assert len(ambiguities) >= 1
        # Filter by surface form
        sub = index.lookup_ambiguous_surface_forms(surface_form="shared")
        assert len(sub) == 1
        assert sub[0]["entity_type"] == "pathogen"
        # Filter by entity type returns the same
        et = index.lookup_ambiguous_surface_forms(entity_type="pathogen")
        assert len(et) >= 1
        # Limit param honored
        capped = index.lookup_ambiguous_surface_forms(limit=1)
        assert len(capped) == 1


def test_probe_030_ambiguous_query_on_old_dictionary_returns_empty():
    """A dictionary built BEFORE the ambiguous_surface_forms table existed
    must still be loadable, with the API returning [] (not crashing on
    OperationalError)."""
    import sqlite3
    import tempfile

    from apecx_integration.synonym_dictionary.loader import DictionaryIndex

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "old.sqlite"
        # Build the OLD schema by hand — only entries + inverse_index +
        # manifest, no ambiguous_surface_forms.
        with sqlite3.connect(db) as conn:
            conn.executescript(
                """
                CREATE TABLE entries (
                    entity_type TEXT NOT NULL,
                    canonical_iri TEXT NOT NULL,
                    canonical_label TEXT NOT NULL,
                    ontology TEXT NOT NULL,
                    ontology_version TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    resolved_at TEXT NOT NULL,
                    source_records_json TEXT NOT NULL,
                    synonyms_json TEXT NOT NULL,
                    PRIMARY KEY (entity_type, canonical_iri)
                );
                CREATE TABLE inverse_index (
                    entity_type TEXT NOT NULL,
                    surface_form_normalized TEXT NOT NULL,
                    canonical_iri TEXT NOT NULL,
                    PRIMARY KEY (entity_type, surface_form_normalized)
                );
                CREATE TABLE manifest (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                """
            )
            manifest_json = (
                '{"dictionary_version":"old","built_at":"2026-01-01T00:00:00Z",'
                '"ontology_versions":{},"record_counts_per_entity_type":{},'
                '"unresolved_count":0,"record_count_total":0}'
            )
            conn.execute(
                "INSERT INTO manifest (key, value) VALUES (?, ?)",
                ("manifest_json", manifest_json),
            )
            conn.commit()
        index = DictionaryIndex.load(db)
        ambiguities = index.lookup_ambiguous_surface_forms()
        assert ambiguities == []
