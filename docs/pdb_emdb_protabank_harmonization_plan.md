# Harmonization plan — PDB · EMDB · ProtaBank bridge · reusable linker

> **STATUS: DEFERRED (2026-06-22) — blocked on SOURCE-index reingestion.** PDB harmonization, the ProtaBank bridge, and EMDB all require fields absent from the readable Globus index; adding them needs a SOURCE-index reingest we cannot perform (no write access). EMDB was already deferred (§3); the whole task is now deferred pending reingestion. The detailed, ready-to-execute PDB plan is in `pdb_harmonization_implementation_plan.md`.

**Status:** planning only. No code changed by this document.
**Scope:** bring **PDB** into full harmonization and republish its DEST index; **bridge ProtaBank** for taxon retrieval through PDB; ship a **reusable third-party-DB linker**. **EMDB harmonization is DEFERRED** (gap analysis in §3). Cross-repo: `apecx-harvesters` (loaders/pipeline) + `apecx-mcp-integration` (consumers/retrieval/linker).
**Verification:** every API/field claim below was checked live this session (probes in §7); fields confirmed absent are marked **[verified-absent]**.

---

## 0. Locked decisions

| Item | Value |
|---|---|
| PDB DEST index (writable) | `857bc08e-5f35-4e8d-8db1-c505419cb5d6` |
| EMDB DEST index (writable) | `79058f1d-3086-4ee4-ad1a-8671b60831a2` — **stays empty; EMDB deferred** |
| Readable aggregate (stub source) | `e74bf12a-d0dd-4d19-a965-03f4936db851` (anonymous read) |
| ProtaBank | already a harmonized index (`protabank`); UniProt-keyed, not taxon-findable. **UUID conflict to reconcile**: code `be999b57-88c4-4aff-a883-4b96c57b66cc` vs doc `9e902471-9c77-49d3-a12c-516cc0808c3b` |
| Homology modeling | out of scope |
| EMDB | **DEFERRED** — harvester strips the fields needed for harmonization (§3) |

**Hard constraint:** we have **write access only to the DEST indices we own**. The SOURCE search index is **read-only / unwritable**. Therefore no harmonization step may "enrich the source index and re-ingest the source." The only writes allowed are to the DEST indices.

---

## 1. Why EMDB is deferred and PDB is not (the field-availability truth)

Checked against the **readable** aggregate `e74bf12a`, not the API:

| Field in the Globus index | EMDB (`emdb:EMD-34119`) | PDB (`pdb:7YV7`) |
|---|---|---|
| `natural_source` / `ncbi` / `organism` | **absent [verified-absent]** | absent |
| `scientific_name` (organism **name**) | **absent [verified-absent]** | **present** ("Coxsackievirus A16") |
| `ncbi_taxonomy_id` (taxid) | absent | **absent [verified-absent]** |
| `subjects[].valueUri` (taxon IRI) | absent | absent |
| `relatedIdentifiers` (PDB cross-ref) | **present** | present |

- **EMDB in the index carries *zero* taxonomic signal** except the PDB cross-reference. The rich `natural_source.organism.ncbi` exists in the **direct EBI API** but the harvester drops it before ingest, so it is not reachable from the index. EMDB cannot be taxon-harmonized from the index → **deferred**.
- **PDB carries the organism *name*** (`scientific_name`), which is enough to resolve to an NCBITaxon IRI via the synonym dictionary. → **PDB proceeds**, index-only, no source write.

---

## 2. Deliverable 1 — PDB harmonization + ProtaBank bridge (ACTIVE)

### 2.1 Source-of-record (permission-safe)
**Read** PDB records from the readable aggregate `e74bf12a` (they carry `pdb.polymer_entities[].scientific_name`); **resolve** organism name → NCBITaxon IRI via the synonym dictionary (`lookup_entity`); **write** the harmonized records to the PDB DEST index `857bc08e…`. No SOURCE-index write. Reuses `harmonize_index()` / `publish_records()` / `to_gmetalist()` / collision+drift guards / `wait_for_ingest`.

**Optional precision upgrade (defer unless API-read→DEST-write is sanctioned):** re-read RCSB GraphQL (`https://data.rcsb.org/graphql`) to capture `rcsb_entity_source_organism.ncbi_taxonomy_id` (exact taxid, avoids name-match ambiguity). The current PDB parser (`apecx-harvesters/.../loaders/pdb/parser.py:133`) extracts only `scientific_name`; extend it to also read `ncbi_taxonomy_id`. *Verify the exact GraphQL field name against the RCSB schema before relying on it.*

### 2.2 Taxon stamping (reuse + the strain requirement)
- Resolve each polymer-entity organism → `subjects[].valueUri = http://purl.obolibrary.org/obo/NCBITaxon_<id>`; **multi-chain fan-out** (multiple organisms → multiple Subjects).
- **Species-ancestor stamping** reused (`TaxonSpeciesMapStep` / `taxon_species` table) so strain records roll up to species.
- **Strain-level retention:** keep the strain IRI *and* its ancestors so the strain-disclosure retrieval (§2.4) can compute match distance. Decision pending: **stamp full lineage** (bigger index, simpler retrieval) vs **query-time climb** against the taxon tree (lean index, retrieval owns the walk) — leaning query-time-climb.

### 2.3 ProtaBank bridge (the real payoff of PDB)
ProtaBank records key on **UniProt**, so they're invisible to taxon search today. Stamp **PDB↔UniProt** into PDB's `alternateIdentifiers` (RCSB exposes the UniProt xref), then join **ProtaBank.UniProt → PDB(harmonized).UniProt → PDB.taxon-IRI**, making ProtaBank taxon-retrievable through PDB. Records whose UniProt has no harmonized PDB structure are honestly reported as unbridgeable (no fabrication).

### 2.4 Strain-aware retrieval + full disclosure (special-attention item)
Most strains have no exact structure, so structural retrieval climbs the taxon tree and **discloses the match level**:
- **Exact strain hit** → "perfect structural match for strain X."
- **Species / sibling-strain hit** → use it, disclose: *"No structure for strain X; using structure(s) for strain Y / species-level Z."*
- **Genus-only hit** → disclose the rank.
- **No hit** → loud "no structural evidence found" (never silent, never invented).
Reuses the species-ancestor stamping + the synonym-dict NCBI taxon tree + the existing "How to proceed" degrade-loud disclosure surface in `viral_epitope_analysis`. Homology modeling is the documented future upgrade for the no-exact-match case.

### 2.5 Consumer rewire
Move `pdb` from `_AGGREGATE_SERVED` → the taxonomy-index set in `harmonized_search.py`; point it at `857bc08e…`; delete the PDB `scientific_name`-facet workaround in `structural_query.py` in favor of the uniform `subjects.valueUri` filter. **Keep `emdb` on the stub** (deferred). Keep the stub code path for not-yet-harmonized DBs.

### 2.6 Real-data acceptance criteria (decide on VALUES)
1. Anonymous query of `857bc08e…` returns PDB records with populated `subjects[].valueUri`; **measured** name→IRI resolution rate reported (not assumed).
2. A known structure (e.g. CHIKV/dengue PDB id) resolves to the correct NCBITaxon IRI.
3. ProtaBank: a taxon query surfaces ProtaBank records bridged through PDB, with the link shown (`ProtaBank → UniProt Pxxxxx → PDB id → taxon`); unbridged records reported honestly.
4. Strain disclosure: a strain with an exact structure reports "perfect"; one without reports the specific fallback strain + level; a no-structure organism degrades loud.
5. `viral_epitope_analysis` structural leg + `harmonized_search` return same-or-better PDB results via the DEST index vs the stub (regression on real viruses).

---

## 3. EMDB — DEFERRED: gap analysis + exact API references to the missing data

**Root cause:** the EMDB harvester strips the harmonization-critical fields *before ingest*, so the readable index has no organism/taxid for EMDB. Fixing it in place is blocked by the read-only SOURCE-index constraint.

### 3.1 Gaps (what blocks EMDB harmonization)
| # | Gap | Evidence |
|---|---|---|
| G1 | **Harvester drops organism/taxid.** `_parse_entry` keeps only `sample.name` free text; never reads `natural_source`. | `apecx-harvesters/src/apecx_harvesters/loaders/emdb/parser.py:88-90` (`_parse_description` = `sample.name.valueOf_` only) |
| G2 | **Index therefore has no EMDB taxonomic signal.** | `e74bf12a` EMD-34119: `natural_source`/`ncbi`/`organism`/`scientific_name`/`valueUri` all **[verified-absent]** (§7) |
| G3 | **Can't re-ingest the SOURCE index** to add the field (no write perms). | user constraint |
| G4 | **EMDB taxon coverage is intrinsically partial** even with the fix. | sample n=16: direct-taxid 9/16, fitted-PDB 9/16, **neither 6/16** (§7) |
| G5 | **EMDB DEST index `79058f1d…` stays empty** until G1–G3 resolved. | by decision |

### 3.2 Exact API calls / references to the MISSING data (for whoever un-defers EMDB)
**(a) Organism + NCBI taxid — the field the harvester must capture:**
```
GET https://www.ebi.ac.uk/emdb/api/entry/{EMD-ID}
```
JSON location (walk the sample tree — sub-keys vary by supramolecule/macromolecule type):
```
sample.supramolecule_list.<*_supramolecule>[].natural_source.organism.ncbi      # int taxid
sample.supramolecule_list.<*_supramolecule>[].natural_source.organism.valueOf_  # name
sample.macromolecule_list.<*_macromolecule>[].natural_source.organism.{ncbi, valueOf_}
```
Live-confirmed (EMD-34119): `natural_source: {database: "NCBI", organism: {ncbi: 31704, valueOf_: "Coxsackievirus A16"}}`.
**Robust extraction:** recursively collect every `natural_source.organism.ncbi` in the sample tree (multi-component → multiple taxids → fan-out to multiple `subjects[].valueUri`).
**ANTI-PATTERN — do not use:** `…recombinant_expression.organism` (the *expression host*, e.g. E. coli for a human protein) — it co-occurs with `natural_source` in the same entry and mislabels the organism.

**(b) Fitted-PDB cross-ref — the proxy-fallback key (already parsed):**
```
GET https://www.ebi.ac.uk/emdb/api/entry/{EMD-ID}
→ crossreferences.pdb_list.pdb_reference[].pdb_id
```
Already extracted at `apecx-harvesters/.../loaders/emdb/parser.py:177-191` (emitted as a `relatedIdentifier`, `relationType=IsSourceOf`).

**(c) Corpus sizing before un-deferring (set the real EMDB success bar):** the EMDB count endpoint needs the correct query shape — my `https://www.ebi.ac.uk/emdb/api/search/*?rows=0&wt=json` returned a bare list, not a `{count}` object, so the n=16 figures are **indicative only**. Work out the correct search/count query (base `https://www.ebi.ac.uk/emdb/api/search/`) and report the true `natural_source.organism.ncbi` fill rate + fitted-PDB rate before committing.

### 3.3 Un-defer prerequisites (all required)
1. **Fix the EMDB harvester** (`parser.py`) to capture `natural_source.organism.ncbi` per §3.2(a) — an `apecx-harvesters` change with its own unit test against a captured fixture that *contains* `natural_source` (the current fixture `emdb_EMD-74041.json` does **not** — capture a richer one).
2. **A permission-safe write path:** since the SOURCE index is unwritable, EMDB harmonization must go **API-read → DEST-write** (read EBI API with the fixed parser, resolve to IRI, write `79058f1d…`). Confirm this path is sanctioned.
3. **Set the EMDB success bar** from §3.2(c)'s real fill rate; below it, keep EMDB on the stub.

### 3.4 Interim (until un-deferred)
EMDB stays **stub-served** (aggregate + `publisher.name` + freetext). Optionally, a *query-time* PDB-proxy in the structural-retrieval leg can lift an EMDB hit's taxon from its fitted PDB (via the harmonized PDB index) **without harmonizing EMDB** — bounded by fitted-PDB coverage (~56% in-sample), disclosed as proxy-derived. This is retrieval-side only; no EMDB ingest.

---

## 4. Deliverable 2 — reusable third-party-DB module (both options)

**Precondition:** centralize the `_INDEX_UUIDS` / `_HARMONIZED_FILTER` registry (currently duplicated across `harmonized_search_execute_step.py`, `epitope_resolve_step.py`, `evidence_review_synthesis_step.py`) into one extensible registry: `name → {dest_uuid | aggregate+publisher, filter-spec, primary-key-type: taxon-IRI | UniProt | PDB | …}`.

- **Option A — repository-side onboarding** (maintainer adds a DB to the harmonized corpus): harvest → harmonize (taxon-IRI + cross-ref stamping) → own DEST index → registry entry. PDB goes through this. Outcome: a permanently taxon-searchable source for everyone.
- **Option B — user-side runtime pull + join** (on-demand, no republish): a reusable step a user drops into a custom pipeline to pull a third-party DB (live API or its existing index) and **join it to the harmonized corpus at runtime** — on the cross-ref substrate (taxon-IRI for organism rollup; DataCite `relatedIdentifiers`/`relationType` + typed `alternateIdentifiers` like UniProt/PDB for object-level links). No infrastructure pre-built. ProtaBank-via-PDB is the canary; the EMDB query-time proxy (§3.4) is an instance.

Both share the **object cross-reference reader** — the genuinely new consumer code: read `relatedIdentifiers`/`relationType` + typed `alternateIdentifiers`, which the harvesters already produce but consumers ignore today (`_datacite.py` reads `relatedIdentifiers` only for DOI). DataCite `relationType` is the standard substrate.

---

## 5. Reuse map

| Component | Reuse |
|---|---|
| harvest/harmonize/publish/collision/drift/`wait_for_ingest` | ~100% (source-agnostic) |
| PDB loader + DataCite parse | ~100% (exists) |
| PDB name→IRI + species-ancestor + strain stamping | ~90% (one `OrganismSlot` + reuse `taxon_species`) |
| Strain-tree-climb + match-level disclosure | new (reuses taxon tree + disclosure surface) |
| ProtaBank bridge (UniProt join) | new consumer code (object cross-ref) |
| Reusable registry (Option A/B) | reuse-of-3-dupes (centralize) + new object-cross-ref reader |
| **EMDB** | **deferred — harvester fix + API-read→DEST path required** |

---

## 6. Open questions / risks
1. **PDB precision:** index name-only vs RCSB-taxid (§2.1) — accept name→IRI for v1, taxid as upgrade?
2. **Lineage-stamp vs query-time-climb** for strain disclosure (§2.2).
3. **ProtaBank UUID conflict** (`be999b57…` vs `9e902471…`) + is its harvest actually populated?
4. **ProtaBank↔PDB coverage:** only UniProt-mapped ProtaBank records bridge; report the remainder honestly.
5. **Writer-role** confirmation on `857bc08e…`; canonical harvester dir (`apecx-harvesters` vs `apecx-harvesters-work`).
6. **PDB name→IRI resolution rate** — measure; the synonym dict's coverage of PDB `scientific_name` spellings is the main quality risk.

---

## 7. Verification evidence (probes run this session)
- **EMDB direct API has the taxid:** `GET https://www.ebi.ac.uk/emdb/api/entry/EMD-34119` → `natural_source.organism.ncbi = 31704` (Coxsackievirus A16). [live]
- **EMDB harvester hits that same endpoint** (`loaders/emdb/retrieve.py:13` `_API_BASE = "https://www.ebi.ac.uk/emdb/api/entry"`) **but its parser drops it** (`parser.py:88-90`). [code]
- **Readable Globus index `e74bf12a`:** `emdb:EMD-34119` → no `natural_source`/`ncbi`/`organism`/`scientific_name`/`valueUri` (only `relatedIdentifiers`); `pdb:7YV7` → `scientific_name` present, no `ncbi_taxonomy_id`, no `valueUri`. [live via `search.api.globus.org/v1/index/{e74bf12a}/search`]
- **Coverage sample (n=16, EMD ids spread across the range):** direct-taxid 9/16, fitted-PDB 9/16, both 8, **neither 6** — complementary sets, ~38% taxon-less floor. **Indicative only** (small sample; correct count query still needed). [live]
