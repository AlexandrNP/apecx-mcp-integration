# Harmonized-Search → DEST Migration Plan (Pass 1 + Pass 2)

**Status:** DRAFT — 2026-06-09.
**Authoring context:** Post-SC-B audit surfaced that `harmonized_search`
queries the original SOURCE Globus indices, not the harmonized DEST
indices, AND that no DEST record carries `subjects.valueUri` because
Phase F (`republish_with_canonical.py`) was designed but never written.
**Cross-repo touchpoints:** `apecx-mcp-integration/src/apecx_integration/composition/`
(MCP-side filter + UUID rewrite) and
`apecx-harvesters-work/src/apecx_harvesters/pipeline/` (republish module).
**Companion docs:**
- `apecx-harvesters-work/design/ONTOLOGY_ENRICHMENT_PLAN.md` (Phase F+G origin)
- `apecx-harvesters-work/design/ONTOLOGY_TASKS.md` (OE-F1..G10 task IDs)
- `apecx-harvesters-work/design/SYNONYM_COMPLETENESS_PLAN.md` (SC-A/B/C dictionary)

---

## 0. The problem in one sentence

`harmonized_search` queries the upstream SOURCE Globus Search indices
with per-source field filters; the harmonized DEST indices are
populated but have empty `subjects[]`; one observable consequence is
that `harmonized_search("Influenza A virus", index="bvbrc_genome")`
returns **0** hits today (the `Species` field carries the renamed
NCBI taxon `Alphainfluenzavirus influenzae`) while a uniform
`alternateIdentifiers.alternateIdentifier="11320"` filter on the DEST
index returns **114,633**.

## 1. Empirical evidence (probed 2026-06-09)

### 1.1 UUID mismatch (single file)

`apecx-mcp-integration/src/apecx_integration/composition/steps/harmonized_search_execute_step.py`
`_INDEX_UUIDS` (line ~93) ≡ `apecx-harvesters-work/src/apecx_harvesters/pipeline/harmonize.py`
`SOURCE_REGISTRY` keys (lines 46-56). It does **not** match
`DEST_REGISTRY` values (lines 61-71). Verbatim from the dest probe:

```text
SOURCE violin_pathogen          total=    217  fields=[VIOLIN_c_pathogen_id, NCBI_Taxonomy_ID, ...]   subjects=(empty)
DEST   violin_pathogen          total=    217  fields=[creators, titles, alternateIdentifiers, ...]    subjects=(empty)
```

Both 217 records; SOURCE carries raw VIOLIN field names, DEST carries
DataCite-shape; **neither** populates `subjects`.

### 1.2 The renamed-taxon failure mode

`bvbrc_genome` DEST holds records with `bvbrc_genome.Species ==
"Alphainfluenzavirus influenzae"` (NCBI's renamed taxon for Influenza
A virus). The current MCP filter map sets `bvbrc_genome → Species →
label` and feeds it the dictionary's canonical label "Influenza A
virus" → match count **0**.

The cross-reference that **does** work today, on the same DEST data:

```text
filter: alternateIdentifiers.alternateIdentifier="11320" (NCBI-Taxonomy)
        → total=114,633 records
```

This is the Pass 1 fix: every DEST record across the 8
NCBI-Taxonomy-anchored sources already carries the NCBI taxon id under
`alternateIdentifiers`. A uniform filter shape replaces 9 per-source
field maps without any republish.

### 1.3 Phase F never landed

`apecx-harvesters-work/src/apecx_harvesters/pipeline/` directory
listing confirms `republish_with_canonical.py` does not exist; the
schema fields (`Subject.subjectScheme/schemeUri/valueUri`) were added
in commit `f722389` and Globus auto-indexes them as facets (verified
2026-06-08 smoke probe), but no per-source enrichment pass has run.
That's Pass 2.

---

## 2. Pass 1 — Switch harmonized_search to DEST + uniform filter

**Goal:** the MCP tool queries the harmonized DataCite-shaped
destination indices, uses a single uniform filter shape that works on
data that already exists, and stops silently returning 0 for renamed
taxa.

**Non-goals:** no republish, no schema migration, no dictionary changes.

### 2.1 Scope

| File | Change |
|---|---|
| `composition/steps/harmonized_search_execute_step.py` | Rewrite `_INDEX_UUIDS` → DEST UUIDs. Rewrite `_HARMONIZED_FILTER` to `{field: "alternateIdentifiers.alternateIdentifier", shape: "taxon_id"}` for the 8 NCBI-Taxonomy-anchored sources; add per-record field-path override for ProtaBank. Add `advanced=True` to the post_search payload (advanced filters require it). |
| `composition/steps/harmonized_resolve_step.py` | Add `index_target: "source" \| "dest"` config field (default `"dest"`) so the regression is reproducible by setting `"source"`. Plumb it into the resolve step's plan output so execute reads from one source of truth. |
| `composition/workflows/harmonized_search/steps/harmonized_search_execute.yml` | Pin `dest_registry_version` for forward compat (used by Pass 2). |

### 2.2 Tests

| Test | File | Real-data assertion |
|---|---|---|
| `test_dest_uuids_match_harvester_registry` | `tests/unit/test_harmonized_search_dest_alignment.py` (NEW, unit) | Imports `DEST_REGISTRY` from `apecx_harvesters.pipeline.harmonize` and asserts every value matches a value in `_INDEX_UUIDS`. **No network.** Catches drift if either side mutates the table. |
| `test_pass1_regression_influenza_a_genome_count` | `tests/integration/test_harmonized_search_dest_live.py` (NEW) | Term "Influenza A virus" → bvbrc_genome harmonized leg must return ≥ **100,000** hits (probed 114,633 today). Pre-Pass-1 baseline was 0. **Headline regression test.** |
| `test_pass1_renamed_taxon_alphainfluenzavirus_matches_influenza_a` | same file | Term "Alphainfluenzavirus influenzae" resolves to NCBITaxon:11320; bvbrc_genome harmonized leg returns the SAME hit count as "Influenza A virus". Pins the canonical-IRI-bridges-NCBI-renames property. |
| `test_pass1_uniform_filter_works_on_8_sources` | same file | For NCBITaxon:11320, every NCBI-Taxonomy-anchored DEST index (8 of 9 — excludes ProtaBank) returns either 0 (taxon legitimately not present) or ≥ 1 hits, AND no exception. |
| `test_pass1_hitl_paused_envelope_unchanged` | same file | Term "RSV" → paused envelope with 6 candidates (per SC-A5b), zero Globus queries fired (assert via response shape, not network mocks). Regression guard on the HITL gate. |
| `test_pass1_protabank_falls_back_to_freetext` | same file | ProtaBank has no NCBI-Taxonomy alt id; harmonized leg degrades to `q=<canonical_label>` and reports `filter_shape: "label_freetext"` in the envelope `data`. |
| `test_pass1_index_target_source_reproduces_old_behavior` | `tests/unit/test_harmonized_resolve_step.py` (EXTEND) | With `index_target="source"`, the plan emits the OLD SOURCE_REGISTRY UUIDs. Lets the old probe set run against the old path for delta measurement. |
| `scripts/measure_pass1_delta.py` | NEW operator script (not a unit test) | Runs a 30-query probe set (the 30-name virology list from `mu_virus_list.txt` plus 5 known-renamed taxa) twice — once with `index_target="source"`, once with `"dest"` — and writes `docs/pass1_delta_report_<date>.md`: per-term { source_hits, dest_hits, delta_pct }. **Operator-facing receipt.** |

### 2.3 Acceptance criteria

| # | Criterion | How verified |
|---|---|---|
| **PA1-1** | All 9 `_INDEX_UUIDS` values are present in `DEST_REGISTRY.values()` | `test_dest_uuids_match_harvester_registry` |
| **PA1-2** | `harmonized_search("Influenza A virus")` → bvbrc_genome returns ≥ 100,000 hits | `test_pass1_regression_influenza_a_genome_count` |
| **PA1-3** | A renamed-taxon query ("Alphainfluenzavirus influenzae") returns the same canonical-IRI hit count as the historic name | `test_pass1_renamed_taxon_alphainfluenzavirus_matches_influenza_a` |
| **PA1-4** | All 8 NCBI-Taxonomy sources accept the uniform filter shape without `GlobusAPIError` | `test_pass1_uniform_filter_works_on_8_sources` |
| **PA1-5** | HITL pause path still fires for SC-A5b ambiguous surface forms (RSV, hepatitis virus, marburg virus) | `test_pass1_hitl_paused_envelope_unchanged` + matches the existing 5/70 ambiguous baseline in `mu_virus_list_baseline.jsonl` |
| **PA1-6** | The 30-query delta report shows ≥ 70% improved, ≤ 5% regressed | `scripts/measure_pass1_delta.py` output committed to `docs/` |
| **PA1-7** | Existing 9 harmonized_search unit tests + workflow integration test all pass | `pytest tests/unit/test_harmonized_*.py tests/integration/test_harmonized_search_workflow.py` |

**Ships when:** PA1-1..PA1-7 green AND the operator delta report is reviewed by the user.

---

## 3. Pass 2 — Author Phase F (`republish_with_canonical.py`) + run OE-G1..G9

**Goal:** every DEST record carries `subjects[]` populated with one
`Subject(valueUri=NCBITaxon:..., subjectScheme="NCBI Taxonomy",
schemeUri=..., subject=<label>)` per resolved entity, written by
re-running the published records through the dictionary resolver and
re-ingesting them. `_HARMONIZED_FILTER` then collapses to one row per
source: `{field: "subjects.valueUri", shape: "iri"}`.

**Non-goals:** no schema changes (already done in commit `f722389`),
no new ontologies beyond NCBI Taxonomy (VO/ChEBI/etc. are Phase G2),
no `harmonize.py` refactor.

### 3.1 Scope

| File | Change |
|---|---|
| `apecx-harvesters-work/src/apecx_harvesters/pipeline/republish_with_canonical.py` (NEW, ~250 lines) | Per OE-F1: reads DEST via `globus_index_records(dest_uuid)`, deserializes to the registered DataCite subclass with `strict=False`, calls the resolver adapter, re-ingests via `to_gmetalist` + `client.ingest`. Records skipped-record log + canonical_uri stability assertions. |
| `apecx-harvesters-work/src/apecx_harvesters/pipeline/canonical_resolver_adapter.py` (NEW, ~120 lines) | Wraps `apecx_harvesters.dict_reader.lookup_entity` (NOT apecx-mcp-integration — the thin lookup package was created for this). Maps per-source entity slots → `Subject` instances per `RESOLUTION_SURFACE.md`. Multi-entity records (AntiviralDB virus+drug, VIOLIN:Vaccine pathogen+vaccine) get N Subject entries. |
| `apecx-harvesters-work/src/apecx_harvesters/scripts/republish_with_canonical.py` (NEW, CLI) | Driver: `python -m apecx_harvesters.scripts.republish_with_canonical <dest-uuid> [--max-skipped-pct 1.0] [--dry-run]`. |
| `apecx-harvesters-work/src/apecx_harvesters/pipeline/globus_source.py` | Add `globus_index_records(dest_uuid)` if not present — mirror of existing `globus_index_source` but reading DataCite-shaped records. |
| `apecx-mcp-integration/src/apecx_integration/composition/steps/harmonized_search_execute_step.py` | Collapse `_HARMONIZED_FILTER` to `{field: "subjects.valueUri", shape: "iri"}` per source. Bump `dest_registry_version` to `"v2-subjects-populated"`. |

### 3.2 Phased rollout (OE-G1 → OE-G9)

| Stage | Source | Records | Why this order |
|---|---|---:|---|
| OE-G1 | AntiviralDB | 35 | smallest, validates strict-round-trip risk |
| OE-G2 | VIOLIN:Pathogen | 217 | validates cross-ref-via-alternate-id short-circuit |
| OE-G3 | BVBRC:Epitope | 442 | first BVBRC parser path |
| OE-G4 | ProtaBank | 1,643 | first non-NCBI-Taxonomy primary slot (UniProt) |
| OE-G5 | VIOLIN:Vaccine | 3,507 | first multi-slot record (pathogen + vaccine) |
| OE-G6 | VIOLIN:Gene | 4,063 | parallelizable with G5 |
| OE-G7 | BVBRC:Protein_Structure | 4,566 | parallelizable |
| OE-G8 | BVBRC:Protein | 24,902 | parallelizable |
| OE-G9 | BVBRC:Genome | 745,917 | run alone; capacity-bound |

### 3.3 Tests

| Test | File | Type | Real-data assertion |
|---|---|---|---|
| `test_resolver_adapter_constructs_against_prod_dict` | `apecx-harvesters-work/tests/test_republish_adapter.py` (NEW) | unit | Adapter loads with `~/.apecx/dictionary/dictionary.sqlite`, returns a `Callable[[DataCite], DataCite]`, smoke-resolves one synthetic record. Pinned by `dictionary_version`. |
| `test_strict_false_roundtrip_per_source` | same file | unit × 9 | For each registered DataCite subclass, take a real DEST record (cached fixture per source), deserialize with `strict=False`, re-serialize, assert structural equality. **Catches OE-F1's known strict-round-trip risk before scale.** |
| `test_canonical_uri_stability` | same file | unit | For every record in a 50-record sample per source, `original.canonical_uri == republished.canonical_uri`. BVBRC:Genome especially — `PrivateAttr` subject-keyed fix was load-bearing. |
| `test_subjects_populated_per_resolution_surface` | same file | unit | A synthetic VIOLIN:Vaccine record (pathogen + vaccine) round-trips with subjects = `[Subject(subjectScheme="NCBI Taxonomy", valueUri="NCBITaxon:..."), Subject(subjectScheme="Vaccine Ontology", valueUri="VO_...")]`. |
| `test_oe_g1_antiviraldb_republish_e2e` | `apecx-harvesters-work/tests/integration/test_republish_antiviraldb_e2e.py` (NEW) | live-Globus | Full republish loop on AntiviralDB DEST (35 records). Pre/post anonymous total = 35. ≥ 80% of records carry ≥ 1 `Subject` with `subjectScheme="NCBI Taxonomy"`. Skipped-record log < 1%. |
| `test_oe_g1_capstone_query_facet_works` | same file | live-Globus | After OE-G1 runs, `subjects.valueUri:"<NCBITaxon:11320>" AND subjects.subjectScheme:"NCBI Taxonomy"` advanced filter returns ≥ 1 hit on AntiviralDB DEST. Pins the facet shape end-to-end. |
| `test_idempotency_re_run` | same file | live-Globus | Running OE-G1 a second time produces identical `canonical_uri` set, identical record counts. **Idempotency** is what makes Pass 2 safe to retry per-source. |
| `test_skipped_record_log_format` | `apecx-harvesters-work/tests/test_republish_skipped_log.py` (NEW) | unit | Inject a record that fails resolution; skipped log carries `{canonical_uri, exception_type, surface_forms_attempted, dictionary_version}`. |
| `test_oe_f2_lifecycle_benchmark` | `apecx-harvesters-work/scripts/benchmark_republish.py` (NEW) | operator script | On 1000 synthetic BVBRC:Protein records: per-record wall-clock, `model_dump`+`model_validate` overhead, OLS-call latency (when fuzzy fallback fires). Projects OE-G9 (Genome) wall-clock cache-cold vs cache-warm. |
| `test_pass2_uniform_filter_per_source` | `apecx-mcp-integration/tests/integration/test_harmonized_search_uniform_filter.py` (NEW) | live-Globus, gated post-Pass-2 | After Pass 2 lands per-source, `_HARMONIZED_FILTER[source]["field"] == "subjects.valueUri"`. For NCBITaxon:11320, hit count via `subjects.valueUri` equals hit count via `alternateIdentifiers.alternateIdentifier="11320"` (the Pass 1 path). |
| `test_oe_g10_capstone_cross_source` | `tests/integration/test_oe_g10_capstone.py` (NEW) | live-Globus | For 3 pinned IRIs (NCBITaxon:11320 Influenza A, NCBITaxon:11103 HepC, NCBITaxon:2697049 SARS-CoV-2), the advanced `subjects.valueUri` query hits ≥ 2 DEST sources non-zero for each. **The "semantic harmonization actually works" receipt.** |

### 3.4 Acceptance criteria

| # | Criterion | How verified |
|---|---|---|
| **PA2-1** | `republish_with_canonical.py` adapter loads against prod dict, round-trips 1 sample per source under `strict=False` | `test_resolver_adapter_constructs_against_prod_dict` + 9× `test_strict_false_roundtrip_per_source` |
| **PA2-2** | OE-G1 (AntiviralDB) completes with 0 FAILED ingest tasks; ≥80% of 35 records carry `subjects.subjectScheme="NCBI Taxonomy"` | `test_oe_g1_antiviraldb_republish_e2e` |
| **PA2-3** | `canonical_uri` stable per record across republish (sample of 10 per source) | `test_canonical_uri_stability` × 9 sources |
| **PA2-4** | Re-running OE-G1 is idempotent: record count + IRIs identical | `test_idempotency_re_run` |
| **PA2-5** | Skipped-record log < 1% of corpus per source | log audit, per-source |
| **PA2-6** | After per-source republish, `subjects.valueUri:"<IRI>"` returns ≥ 1 hit for every source where that taxon is present per OE-C coverage projection (±2pp) | `test_pass2_uniform_filter_per_source` |
| **PA2-7** | OE-G10 capstone: for 3 pinned IRIs, ≥ 2 DEST sources non-zero each via `subjects.valueUri` | `test_oe_g10_capstone_cross_source` |
| **PA2-8** | OE-F2 benchmark projects OE-G9 (Genome, 745k) within tolerable wall-clock (target: < 4 h cache-warm) | `scripts/benchmark_republish.py` report committed |
| **PA2-9** | `_HARMONIZED_FILTER` collapses to a single row per source: `{field: "subjects.valueUri", shape: "iri"}` | code review + unit test asserts shape |
| **PA2-10** | Pass 1's regression test (`test_pass1_regression_influenza_a_genome_count`) still green under the Pass 2 filter shape (proves no information loss across pass transitions) | re-run the Pass 1 suite under Pass 2 code |

**Ships per source** (G1..G9), not as a single big-bang. The gate for
promoting the filter rewrite at the MCP layer is **all 9 sources at
PA2-2..PA2-6 green**. Until then, `_HARMONIZED_FILTER` keeps the Pass 1
shape and reads from `subjects.valueUri` only as a fallback.

### 3.5 Risks called out in advance

| Risk (from `ONTOLOGY_ENRICHMENT_PLAN.md` §"Medium risk") | How a test catches it |
|---|---|
| `strict=True` round-trip breaks mid-batch | `test_strict_false_roundtrip_per_source` × 9 BEFORE any G-stage runs |
| Pydantic round-trip cost at 745k scale | `test_oe_f2_lifecycle_benchmark` — go/no-go gate before OE-G9 |
| OLS rate-limit during resolver fallback | benchmark records p50/p95 latency at the OLS leg; if > target, gate OE-G9 on cache pre-warm |
| Per-source canonical_uri drift | `test_canonical_uri_stability` runs per source before promotion |
| Dictionary version drift mid-republish | adapter pins `dictionary_version` into the skipped-record log + provenance; OE-F0 acceptance asserts version match |
| `Species` field misalignment we caught for Pass 1 reappears for ProtaBank's UniProt path | OE-G4 dedicated stage — ProtaBank is the early canary for non-NCBI primary slot |

---

## 4. Code layout summary

```
apecx-mcp-integration/                    apecx-harvesters-work/
└─ src/apecx_integration/                 └─ src/apecx_harvesters/
   └─ composition/steps/                     └─ pipeline/
      └─ harmonized_search_execute_step.py      ├─ harmonize.py              (already has DEST_REGISTRY)
         │                                      ├─ republish_with_canonical.py  (Pass 2 NEW)
         │  Pass 1: _INDEX_UUIDS → DEST          ├─ canonical_resolver_adapter.py (Pass 2 NEW)
         │  Pass 1: _HARMONIZED_FILTER uniform   └─ globus_source.py          (Pass 2 add globus_index_records)
         │  Pass 2: filter → subjects.valueUri
         │                                   └─ scripts/
         │                                      └─ republish_with_canonical.py (Pass 2 CLI)
         └─ harmonized_resolve_step.py
            Pass 1: +index_target config
```

---

## 5. Suggested execution order

1. **Day 1** — Pass 1 PA1-1..PA1-5 (unit + smoke integration). Read the delta report (PA1-6) WITH the user before promoting.
2. **Day 2** — Author OE-F0/F1 against AntiviralDB only; ship PA2-1..PA2-3 on G1. Pause for review (35-record corpus is the cheap canary).
3. **Day 3-4** — OE-G2..G8 (small/medium sources, parallelizable from G6 onward).
4. **Day 5** — OE-F2 benchmark + OE-G9 (Genome). Gate strictly on the benchmark projection.
5. **Day 6** — collapse `_HARMONIZED_FILTER` to `subjects.valueUri` shape (PA2-9), run OE-G10 capstone (PA2-7), retire the Pass 1 `alternateIdentifiers` fallback.

---

## 6. Implementation log (append-only)

- 2026-06-09 — Plan drafted. UUID mismatch + renamed-taxon failure mode
  verified empirically (114,633 vs 0 on bvbrc_genome / Influenza A).
  Phase F module confirmed absent from
  `apecx-harvesters-work/src/apecx_harvesters/pipeline/`. No code yet;
  Pass 1 awaiting user go-ahead.
