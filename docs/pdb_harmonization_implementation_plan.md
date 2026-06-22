# PDB harmonization + ProtaBank bridge — detailed implementation plan

> **STATUS: DEFERRED (2026-06-22) — blocked on SOURCE-index reingestion.** Every path to a taxon-IRI + UniProt PDB index requires data that is not in the readable Globus index and cannot be added without reingesting the SOURCE index (which we have no write access to), plus the harvester extension (WS1) and the `Subject` reconciliation (T0.1). Until the index is reingested with the extended fields, no part of this is actionable. This document is the ready-to-execute plan for when that unblocks. Do not treat any workstream as in-flight.

**Status:** planning only. No code changed by this document.
**Scope:** harmonize **PDB** (taxon-IRI on its DEST index `857bc08e-5f35-4e8d-8db1-c505419cb5d6`), **bridge ProtaBank** (UniProt→PDB→taxon), and ship **strain-aware structural retrieval with disclosure**. EMDB remains deferred (see `pdb_emdb_protabank_harmonization_plan.md` §3).
**Repos:** `apecx-harvesters` (loaders), `apecx-harvesters-work` (pipeline — **canonical**, confirmed via `git rev-parse`), `apecx-mcp-integration` / `wt-epitope-artifacts` (consumers). All code references below are **verbatim-verified** this session.

---

## 0. BRUTAL TRUTH — four landmines that change the estimate

The generic plan said "~90% reuse, register two registry entries." That is **wrong**. The verbatim code shows:

1. **The pipeline reads from a per-source SOURCE Globus index — PDB has none, and you can't write one.** `harmonize_index()` does `globus_index_source(SOURCE_uuid, parser)` → scroll that index → publish to DEST (`harmonize.py:96-161`). The 9 sources each have a dedicated SOURCE index in `SOURCE_REGISTRY` (`harmonize.py:46-56`). **PDB is not there, the readable aggregate `e74bf12a` is a *mixed* index (all DBs), and you said the SOURCE index is unwritable.** So the standard 4-stage `run_full_harmonization.py` path **does not apply** — PDB needs a *different* driver that reads the live RCSB API (or the aggregate, filtered) and writes only the DEST index.

2. **The ProtaBank bridge cannot be built from the index — the data isn't there.** The bridge needs **PDB↔UniProt**. The RCSB GraphQL query (`retrieve.py:30-51`) requests **only `scientific_name`**; the `PolymerEntity` model (`pdb/model.py:18-36`) has **no UniProt and no taxid**; the parser (`parser.py:129-141`) extracts **only `scientific_name`**; the captured fixture confirms it; and the aggregate index has no UniProt for PDB. **So the bridge — your stated payoff — forces a fresh, extended RCSB harvest. It is impossible index-only.**

3. **Exact taxid is also not in the index.** Same root cause: the query/model/parser never capture `ncbi_taxonomy_id`. From the aggregate you get organism *name* only → name→IRI via the synonym dict (lossy on ambiguous names). Exact taxid (clean) **also** requires the extended live harvest.

4. **`Subject` model divergence between the two repos — a real blocker.** In `apecx-harvesters` the base `Subject` has **only `subject`** (no `valueUri`) — `loaders/base/model.py:306-320`, deliberately deferred per `OPEN_QUESTIONS.md`. But the `apecx-harvesters-work` resolver **constructs `Subject(subject=…, subjectScheme=…, schemeUri=…, valueUri=…)`** (`canonical_resolver_adapter.py:174-191`). These are **separate git repos** (not worktrees). **The taxon-IRI harmonization literally cannot serialize a `valueUri` on the loaders' Subject.** This must be reconciled before any harmonized record can be produced. (Also corrects an earlier claim: `_INDEX_UUIDS` is **not** duplicated in 3 files — it's defined once in `harmonized_search_execute_step.py:114` and *imported* by `epitope_resolve_step.py:125` + `evidence_review_synthesis_step.py:762`. The "centralize the registry" task is smaller than stated.)

**Net:** this is a genuine multi-part feature spanning two harvester repos + the consumers, not a registry add. My honest recommendation is **Path β** (below) because Path α cannot deliver the ProtaBank bridge you asked for.

---

## 1. The decision fork (yours to make)

| | **Path α — aggregate-read, name-only** | **Path β — live RCSB harvest, extended (RECOMMENDED)** |
|---|---|---|
| Source of record | read `e74bf12a` filtered to `publisher=RCSB PDB` | live RCSB GraphQL (`data.rcsb.org/graphql`) |
| Taxon | organism **name** → IRI via synonym dict (lossy) | **exact `ncbi_taxonomy_id`** → IRI (clean) |
| UniProt / ProtaBank bridge | **impossible** (not in index) | **yes** (extend the query) |
| Harvester changes | none | GraphQL + model + parser (WS1) |
| Writes | DEST only | DEST only |
| Delivers your stated goals? | **No** (no bridge) | **Yes** |

**Recommendation: Path β.** Everything below assumes β. If you accept α, the bridge is off the table and we ship a name-only PDB taxon index — say so and I'll re-scope.

---

## 2. Task 0 — blockers to resolve BEFORE any code (no-code; investigation/ops)

| ID | Blocker | Resolve by |
|---|---|---|
| T0.1 | **Two-repo `Subject` divergence.** Which `Subject` does the PDB loader + pipeline use end-to-end? Does `-work` import loaders from `apecx-harvesters`, or vendor its own? | `git -C apecx-harvesters-work grep -n "class Subject"` + trace `parse_protabank`'s import; confirm whether `-work`'s `Subject` has `valueUri`. Decide: extend `apecx-harvesters` `Subject` to add optional `subjectScheme/schemeUri/valueUri`, **or** make PDB a `-work`-native loader. |
| T0.2 | **Writer role** on DEST `857bc08e…` for the confidential client `bbcdba6f…`. | `globus search index show 857bc08e…`; attempt a 1-record test ingest. |
| T0.3 | **Full PDB ID list** (~220k entries) for a complete harvest. | RCSB holdings API `https://data.rcsb.org/rest/v1/holdings/current/entry_ids` (returns all current PDB IDs). |
| T0.4 | **ProtaBank populated?** Its harvest is "not yet wired"; the DEST `be999b57…` (code) vs `9e902471…` (doc) conflict. | Anonymous query `be999b57…` for a record with `alternateIdentifierType=="UniProt"`; reconcile the UUID. |

**T0.1 is a true blocker** — without a `valueUri`-capable `Subject`, no harmonized PDB record can exist.

---

## 3. Workstreams (Path β) — file:line, snippets, tests, ACs

### WS1 — Extend the RCSB harvest to capture taxid + UniProt  [`apecx-harvesters`]
**Edit 1 — GraphQL query** `loaders/pdb/retrieve.py:44-49` (currently):
```python
    polymer_entities {
      rcsb_id
      entity_poly { rcsb_entity_polymer_type }
      rcsb_entity_source_organism { scientific_name }
    }
```
→ add taxid + the UniProt cross-ref:
```python
    polymer_entities {
      rcsb_id
      entity_poly { rcsb_entity_polymer_type }
      rcsb_entity_source_organism { scientific_name ncbi_taxonomy_id }
      uniprots { rcsb_id }            # RCSB UniProt accession(s) for this entity
    }
```
*Verify the `uniprots { rcsb_id }` field name against the live RCSB GraphQL schema before relying on it (introspection: `{__type(name:"PolymerEntity"){fields{name}}}`); it is the documented UniProt linkage but the exact path must be confirmed.*

**Edit 2 — model** `loaders/pdb/model.py:18-36` add to `PolymerEntity`:
```python
    ncbi_taxonomy_id: Annotated[Optional[int], Field(
        description="NCBI taxon id from rcsb_entity_source_organism; None when unrecorded.")] = None
    uniprot_ids: Annotated[list[str], Field(
        description="UniProt accessions linked to this entity (RCSB uniprots.rcsb_id).")] = Field(default_factory=list)
```

**Edit 3 — parser** `loaders/pdb/parser.py:129-141` (`_parse_polymer_entities`):
```python
        organisms = entity.get("rcsb_entity_source_organism") or []
        scientific_name = organisms[0].get("scientific_name") if organisms else None
        ncbi_taxonomy_id = organisms[0].get("ncbi_taxonomy_id") if organisms else None
        uniprot_ids = [u["rcsb_id"] for u in (entity.get("uniprots") or []) if u.get("rcsb_id")]
        entities.append(PolymerEntity(
            entity_id=entity["rcsb_id"], scientific_name=scientific_name,
            ncbi_taxonomy_id=ncbi_taxonomy_id, uniprot_ids=uniprot_ids,
            polymer_type=poly.get("rcsb_entity_polymer_type")))
```

**Test WS1 (real data, no synthetic):** **re-capture** `tests/fixtures/pdb_graphql_6m0j.json` with the extended query (6M0J = SARS-CoV-2 spike RBD + human ACE2 — real multi-organism). New unit test asserts the parser yields `ncbi_taxonomy_id` 2697049 (SARS-CoV-2) and 9606 (human) and the real UniProt accessions (P0DTC2, Q9BYF1). The current fixture **lacks** these fields — it must be re-captured, not hand-edited (hand-editing = synthetic; re-capture from the live API = real).
**AC WS1:** parsing the real 6M0J payload produces both taxids + both UniProt accessions; a synthetic-construct entry (no organism) yields `None`/`[]` without error.

### WS2 — `valueUri`-capable `Subject`  [resolve per T0.1]
If T0.1 says extend `apecx-harvesters` `Subject` (`loaders/base/model.py:306-320`):
```python
    subject: Annotated[str, Field(title="Subject", description="Keywords")]
    subjectScheme: Annotated[Optional[str], Field(default=None)] = None
    schemeUri: Annotated[Optional[str], Field(default=None)] = None
    valueUri: Annotated[Optional[str], Field(default=None)] = None
```
**Test WS2:** round-trip a real harmonized record through `model_dump_json()`/validation with a populated `valueUri`; assert no `extra="forbid"` violation. **AC WS2:** existing loader unit tests stay green (the new fields are optional); a `Subject` with `valueUri` serializes.

### WS3 — PDB harmonization driver (live harvest → IRI + species-ancestor + UniProt → publish DEST)  [`apecx-harvesters-work`]
No SOURCE index ⇒ a **new thin driver** `scripts/harmonize_pdb_live.py`, reusing `PDBHarvester.iter_results` + `publish_records` (`harmonize.py:169-190`) + `to_gmetalist` + `wait_for_ingest`. Taxon stamping is **direct from the taxid** (no name lookup needed):
```python
# for each harvested PDBContainer record:
subjects = list(record.subjects or [])
for ent in record.pdb.polymer_entities:
    if ent.ncbi_taxonomy_id:
        iri = f"http://purl.obolibrary.org/obo/NCBITaxon_{ent.ncbi_taxonomy_id}"
        subjects.append(Subject(subject=ent.scientific_name or str(ent.ncbi_taxonomy_id),
                                subjectScheme="NCBITaxon", schemeUri=_OBO, valueUri=iri))
        sp = species_for_taxon(con, ent.ncbi_taxonomy_id)        # reuse taxon_species hop
        if sp and sp != ent.ncbi_taxonomy_id:
            subjects.append(Subject(..., valueUri=f"...NCBITaxon_{sp}"))
    for up in ent.uniprot_ids:                                    # ProtaBank bridge key
        record.alternateIdentifiers.append(AlternateIdentifier(alternateIdentifier=up,
                                                               alternateIdentifierType="UniProt"))
record = record.model_copy(update={"subjects": subjects})
# then: publish_records([...], client=client, dest_index="857bc08e-...")
```
*Brutal note:* this **bypasses** `_SOURCE_SLOTS`/`make_resolver_for_source` (those resolve a *name* surface form; we have the exact taxid, which is better). The species-ancestor reuse needs the `taxon_species` SQLite from the apecx side — confirm cross-repo access or vendor the hop.
**Test WS3 (real, live-gated):** harvest a real 3-PDB set (6M0J, a CHIKV structure e.g. 3J2W, a dengue structure) live → assert each record has `subjects[].valueUri = NCBITaxon_<exact id>` + species ancestor + UniProt in `alternateIdentifiers`. Then ingest those 3 into `857bc08e…` and **query anonymously** → assert the `valueUri` is filterable.
**AC WS3:** anonymous `subjects.valueUri == NCBITaxon_2697049` filter on `857bc08e…` returns 6M0J; UniProt P0DTC2 present in its `alternateIdentifiers`. Full-scale run: measured taxid-fill rate reported (entries with no source organism are honestly `valueUri`-less, not fabricated).

### WS4 — ProtaBank → PDB UniProt bridge  [`wt-epitope-artifacts`, consumer]
ProtaBank records expose UniProt at `alternateIdentifiers[type==UniProt]`, read by `datacite_identifiers()` (`_datacite.py:94-123`). Add a join: ProtaBank.UniProt → query harmonized PDB DEST `857bc08e…` for `alternateIdentifiers.alternateIdentifier == <UniProt>` → read that PDB's `subjects[].valueUri` (via `datacite_taxon_iris`, `_datacite.py:142-153`). Emit the link with provenance `ProtaBank → UniProt <id> → PDB <id> → <taxon IRI>`.
**Test WS4 (real, both ends):** take a **real** ProtaBank record from `be999b57…` carrying a UniProt accession; resolve it through the harmonized PDB DEST; assert a real PDB structure + its taxon IRI are returned. A ProtaBank UniProt with no harmonized PDB is reported unbridgeable (no fabrication).
**AC WS4:** ≥1 real ProtaBank record becomes taxon-retrievable via the bridge; the link chain is shown; unbridged records are counted honestly.

### WS5 — Consumer rewire (PDB onto the harmonized DEST)  [`wt-epitope-artifacts`]
- `harmonized_search_execute_step.py:114` add `"pdb": "857bc08e-5f35-4e8d-8db1-c505419cb5d6"`; `:95` add `"pdb": {"field": "subjects.valueUri", "shape": "iri"}`.
- `harmonized_search.py:65-68` remove `"pdb"` from `_AGGREGATE_SERVED`; `:46-56` add `"pdb"` to `_TAXONOMY_INDICES`. **Leave `emdb` in `_AGGREGATE_SERVED`** (deferred).
- `structural_query.py:190-227` (`enumerate_organisms` facet pre-pass) — remove from the **PDB** path (now uses the uniform `subjects.valueUri` filter via `client.search(filters=[{"type":"match_any","field_name":"subjects.valueUri","values":[iri]}])`, `client.py:122-152`). **Keep** the EMDB freetext block `:280-287` (EMDB deferred).
- `structural_evidence_step.py:48-53` publisher discriminator stays (PDB still distinguished by publisher within results).
**Test WS5 (real regression):** run the `viral_epitope_analysis` structural leg for a real virus (CHIKV) **before vs after** rewire; assert the post-rewire PDB hit set ⊇ pre-rewire (decide on returned PDB IDs + count, not status). **AC WS5:** equal-or-more real PDB hits; the per-query facet round-trip is gone (one fewer Globus call).

### WS6 — Strain-aware retrieval + disclosure  [`wt-epitope-artifacts`]
**Brutal truth (from the code):** full lineage climbing is **not** available. Only a single **strain→species** hop exists (`taxon_species`, `taxon_species_map_step.py:182-188`); `nodes.dmp` ranks are parsed at build then **discarded** (`hierarchy_loader.py:133-137`); `taxon_hierarchy` has child→parent edges but **no rank column**. So:
- **Feasible now (2 levels):** harmonized PDB carries both the strain IRI and its species-ancestor IRI (WS3). Retrieval: query the exact strain IRI → hit ⇒ "perfect match"; else query the species IRI → hit ⇒ "same species, different isolate: PDB <id> (strain Y)"; else loud "no structural evidence." `lookup_entity`'s `path=="ancestor"` (`lookup.py`) already surfaces ancestor matches with disclosure.
- **Genus+ climbing (deferred extension):** requires persisting rank — add a `taxon_rank` column to `taxon_hierarchy` (or a `taxon_lineage` table) in `sqlite_writer.py:123-126` + a climb query. Scope this as a follow-up; do **not** claim genus-level disclosure until it ships.
**Test WS6 (real):** a virus with a known exact-strain PDB structure → asserts "perfect"; a strain with only species-level structures → asserts the specific fallback strain + "species-level" label; a no-structure organism → loud degrade. All on real PDB data.
**AC WS6:** the disclosed match level matches reality on ≥3 real viruses (one exact, one species-fallback, one none); no fabricated "exact" claims.

---

## 4. Tests — real-data policy (no synthetic data)

- **Unit tests use re-captured REAL API payloads** (the harvesters' own convention — `apecx-harvesters/CLAUDE.md`: "captured mock api payload"). The current `pdb_graphql_6m0j.json` lacks taxid/UniProt → **re-capture it live**; never hand-fabricate the fields.
- **Integration tests hit real backends:** live RCSB GraphQL (WS1/WS3), real ingest to `857bc08e…` + anonymous read-back (WS3), real ProtaBank record from `be999b57…` (WS4), real `viral_epitope_analysis` run (WS5/WS6). Gate live tests on creds/network with `pytest.importorskip`/skipif, never on fabricated data.
- **Unit-mock / integration parity** (workspace rule): any mocked unit (e.g. a fake SearchClient for the driver) must have a matching live integration test recorded in its docstring.

## 5. Dependency graph
```
T0.1 (Subject divergence) ─┐
T0.2 (writer role)         ├─→ WS1 (harvest ext) ─→ WS3 (driver+publish DEST) ─┬─→ WS4 (ProtaBank bridge)
T0.3 (PDB id list)         │   WS2 (Subject.valueUri) ─┘                        ├─→ WS5 (consumer rewire) ─→ WS6 (strain disclosure)
T0.4 (ProtaBank/UUID)  ────┘                                                    └─→ (WS6 also needs WS3's species-ancestor stamping)
```
- **Phase 1 (PDB harmonized to DEST):** T0.1–T0.3 → WS1 → WS2 → WS3. Ships a taxon-IRI + UniProt PDB index. *This is the keystone; do it first.*
- **Phase 2 (payoff):** WS4 (bridge) ∥ WS5 (rewire) → WS6 (disclosure).
- WS6 genus+ climbing = separate follow-up (rank persistence).

## 6. Brutal-truth risks / what I'd cut
- **Biggest risk: T0.1.** If the two repos can't share a `valueUri` Subject cleanly, WS3 stalls. Resolve it first; everything depends on it.
- **RCSB GraphQL field names** (`uniprots.rcsb_id`, `ncbi_taxonomy_id`) are stated from RCSB's schema but **not introspected this session** — verify before WS1.
- **Full PDB harvest is ~220k live GraphQL calls** (batched 200/req ≈ 1100 requests at 10 req/s ≈ minutes, but RCSB may throttle). Budget it; consider an incremental holdings diff later.
- **What I'd cut for a first ship:** genus+ strain climbing (WS6 deferred half) and any pretense of EMDB. Ship Phase 1 + WS4 + WS5 + WS6-2-levels; that delivers the ProtaBank bridge and honest strain disclosure, which is the actual ask.
- **Honest scope correction:** this is *not* "90% reuse." Reuse is high for *publish/ingest/guards* (`publish_records`/`to_gmetalist`/`wait_for_ingest`) and the species-ancestor hop, but WS1 (harvester extension), WS2 (Subject), WS3 (new driver), and WS4/WS6 are genuinely new code. Estimate accordingly.
