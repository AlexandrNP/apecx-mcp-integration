# viral_epitope_evidence_review v2.1 — Detailed Implementation Plan

Companion to `viral_epitope_evidence_review_v2_plan.md` (design rationale + real-data
findings). This doc is the actionable task tree: dependencies, concrete steps, files,
gated real-data tests, and data-based acceptance per task. Cite task IDs (E3-*) in
commits/PRs. Branch: `epitope-evidence-workflow` (apecx) + per-repo branches for
nanobrain/rhea. Nothing pushed without explicit approval (exception: the Rhea fork
main, authorized for E3-4).

---

## Cross-cutting acceptance rules (CC) — apply to EVERY task

- **CC-1 — No empty responses (load-bearing).** A component's happy path MUST return
  non-empty REAL data, and a test MUST assert non-emptiness on real data (`len(x) >= 1`
  / a concrete value), never merely "did not raise". An empty/absent result is
  admissible ONLY as an explicitly NAMED degrade (a non-empty human-readable note in
  the stage report + structured `*_available: false` flag) — and that degrade path
  also gets its own test asserting the note is present and non-empty. "Returns `[]`
  silently" is a test FAILURE.
- **CC-2 — Decide success from output VALUES, not status (G127).** Every step never
  raises in a way that strands the cascade; it degrades loud and passes the bundle
  through. Tests assert on the concrete output value, never on `run()` status.
- **CC-3 — Real-data integration test required for "done."** Mocks allowed only for
  wire-shape unit tests; each mocked behavior has a matching real-dependency
  integration test (gated, auto-skip honest). No mock-only completion.
- **CC-4 — Determinism + caching for external calls.** Immutable sources (PDB, SIFTS)
  cached indefinitely by id; versioned/moving sources (UniProt, IEDB) cached by
  release/TTL with the query date recorded in provenance. Same input → byte-stable
  output (assert across two runs).
- **CC-5 — nanobrain-native.** from_config only; implement `process()`, never
  `execute()`; `extra='forbid'` on new configs; every DirectLink `auto_transfer`
  (config_version 2); framework gaps ship as a nanobrain capability + SKILL + regression
  test.

Each task's **AC** below is in ADDITION to CC-1..CC-5.

---

## Task dependency tree

```
PHASE 1 — scientific-quality core (the compounding chain)
  E3-2 taxon-precise structural query ──────────────┐ (picks the RIGHT structure)
    E3-2.1 datacite_organisms + organism facet      │
    E3-2.2 taxon→species-name resolution            │
    E3-2.3 PDB scientific_name match_any (+ tool)    │
    E3-2.4 EMDB advanced required-token path         │
    E3-2.5 taxon-unresolvable degrade               │
                                                     ▼
  E3-1 biological-assembly SASA ────────────────────┤ (correct accessibility on it)
    E3-1.1 host-fetch assembly (.pdb1) + cache       │
    E3-1.2 PyMOL job: SASA over assembly + chain map │
    E3-1.3 no-assembly degrade to AU (named)         │   needs E3-7 image to verify
                                                     ▼
  E3-3 real functional validation ──────────────────┤
    E3-3.1 SiftsClient (PDB→UniProt + residue bridge)│
    E3-3.2 UniProtClient (residue features)          │
    E3-3.3 IedbClient (epitopes, bonus)              │
    E3-3.4 cross-check + wire into scan seam         │
    E3-3.5 SIFTS numbering fixture + degrades        ▼
                                              E3-8 provenance capture
                                              (records E3-1/2/3 params)

PHASE 2 — automation / reproducibility (parallel to Phase 1)
  E3-4 Rhea auto-setup (independent track)
    E3-4.1 fork: HOST=0.0.0.0 + Dockerfile conda → PUSH fork main
    E3-4.2 apecx-setup Docker _step_rhea (build/run/ingest)  ◄── E3-4.1
    E3-4.3 auto-set RHEA_MCP_URL + daemon-down degrade        ◄── E3-4.2
    E3-4.4 live synthesize_rhea_step integration (closes E2-R) ◄── E3-4.3
  E3-7 PyMOL image build in apecx-setup (independent; UNBLOCKS E3-1 real verify)

PHASE 3 — surface verification (independent)
  E3-5 real MCP client desktop/headless verification
    E3-5.1 minimal stdio MCP client → E3-5.2 e2e test → E3-5.3 doc + mechanism review

PHASE 4 — reliability / conformance hygiene (independent, parallelizable)
  E3-6 model resolver+preflight · E3-9 conserved-sites cache · E3-10 test-isolation
  E3-11 EnvelopeStepConfig · E3-12 G130 doc · E3-13 multi-structure (opt) · E3-14 quality tier (opt)
```

**Critical path:** E3-2 → E3-1 → E3-3 → E3-8. **Unblocks-external-verification:** E3-7
(PyMOL), E3-4 (Rhea). **Parallel any time:** Phase 4 + E3-5.

---

## PHASE 1 — scientific-quality core

### E3-2 — Taxon-precise structural Globus query
Depends: none. Parallel with: E3-4, E3-7, Phase 4. Blocks: E3-1/E3-3 quality.

**E3-2.1 — `datacite_organisms` + organism facet pre-pass.**
- Files: `agents/globus_search/_datacite.py` (+ `datacite_organisms(content)` reading
  `pdb.polymer_entities[].scientific_name`), `agents/globus_search/client.py` (add a
  `facet(field, q, filters)` helper or a `_enumerate_organisms(species_term)` in the
  structural step).
- Steps: facet `pdb.polymer_entities.scientific_name` scoped by the species term →
  return every bucket value whose lowercased text contains the species name.
- Tests (real Globus, gated `_globus_reachable`): `_enumerate_organisms("chikungunya")`
  returns **≥3** spellings INCLUDING `"Chikungunya virus"` and the `S27-African`
  strain variant. **CC-1:** assert the set is non-empty AND contains the canonical name.
- AC: the facet pre-pass returns the real multi-spelling organism set (≥1, canonical
  present); `datacite_organisms` extracts ≥1 organism from a real PDB record.

**E3-2.2 — taxon→species-name resolution.**
- Files: `composition/steps/structural_evidence_step.py` (+ a `_resolve_species_names`
  helper); reuse the `taxon_species` table / strain→species mapping (see
  `strain_species_normalization_shipped`).
- Steps: given `taxon_id` (and/or species terms parsed from `query`), produce the
  species-name string(s) used to scope the facet. Degrade to query-term parsing when no
  `taxon_id`.
- Tests: `taxon_id=37124` → resolves to a name set containing "Chikungunya virus"
  (**non-empty**, CC-1); unknown taxon → falls to query-term path with a named note.
- AC: a real CHIKV `taxon_id` resolves to ≥1 species name string matching the index's
  organism spellings.

**E3-2.3 — PDB structured query (step + MCP tool, lockstep).**
- Files: `structural_evidence_step.py:_search_source`,
  `mcp_surface/tools/harmonized_search.py:_aggregate_served_search`.
- Steps: build `filters=[publisher=RCSB PDB] + match_any(pdb.polymer_entities.scientific_name,
  <enumerated organisms>)`, `q=<protein + structural keywords>`, `advanced=true`.
- Tests (real Globus): for the CHIKV envelope case, the result set's organisms are
  **all CHIKV** (assert no non-CHIKV organism in the top-10 — explicitly assert West
  Nile/other absent) and **total ≥1** (CC-1: non-empty). Before/after fixture pins the
  improvement (free-text included West Nile; structured does not).
- AC: a CHIKV envelope query returns **≥1** structure, **100% CHIKV-deposited**;
  cross-virus false positives = 0.

**E3-2.4 — EMDB advanced required-token path.**
- Files: same two.
- Steps: for `publisher=EMDB`, `advanced` `q` requiring the taxon token in
  `titles.title`/`descriptions.description` AND the structural keyword.
- Tests (real Globus): EMDB CHIKV query returns hits whose title/description all
  contain the taxon token (**non-empty when the corpus has them**; if genuinely 0, a
  NAMED no-hit note per CC-1, not a silent empty).
- AC: EMDB results are taxon-locked; no cross-virus leak.

**E3-2.5 — taxon-unresolvable degrade.**
- Steps: when no species can be resolved, fall back to the current free-text query +
  set a NAMED `structural_note` ("results not taxon-locked: could not resolve a species
  for <query>").
- Tests: a query with no resolvable taxon → result still non-empty OR a named note;
  the note string is present + non-empty (CC-1 degrade branch).
- AC: never a silent unfiltered dump — either taxon-locked results or a named caveat.

### E3-1 — Biological-assembly SASA (accessibility)
Depends: none (code); real-data verify needs E3-7 (image). Parallel with E3-2.

**E3-1.1 — host-fetch biological assembly + cache.**
- Files: `composition/steps/structural_reasoning_step.py:_fetch_structure`.
- Steps: fetch `https://files.rcsb.org/download/{PDB}.pdb1.gz` (assembly 1) to the
  existing `~/.cache/apecx_pymol/` cache; fall back to the AU `.cif` when no `.pdb1`
  (404). Record which was used.
- Tests (network-gated): 2XFB `.pdb1` downloads + caches (**file non-empty**, CC-1);
  a structure with no assembly → AU fallback flagged.
- AC: the assembly file is fetched + cached for a real PDB; the source (assembly vs AU)
  is recorded.

**E3-1.2 — PyMOL job: SASA over assembly + residue→chain-copy map.**
- Files: `docker/pymol/_pymol_job.py`, `composition/steps/_pymol_sasa.py`.
- Steps: load the assembly (or `set assembly,1`), compute per-residue SASA over the
  full oligomer (pinned `dot_solvent=1`/`dot_density=3`), map candidate residues to the
  correct chain copy (author numbering preserved per copy); classify exposed/buried by
  relative-SASA in the ASSEMBLY context.
- Tests (Docker+image-gated, real 2XFB): returns **≥1** classified residue (CC-1);
  assembly-SASA DIFFERS from AU-SASA for ≥1 known interface residue (the whole point);
  byte-stable across two runs (CC-4).
- AC: candidate residues classified in the **biological-assembly** context; ≥1 residue
  whose exposed/buried verdict changes vs the AU (proves the fix is real, not cosmetic).

**E3-1.3 — no-assembly degrade to AU (named).**
- Steps: when no assembly is defined, compute over AU + emit a NAMED note in the
  structural-reasoning stage report ("accessibility computed over the asymmetric unit;
  no biological assembly deposited").
- Tests: a real no-assembly PDB → result non-empty + the AU-caveat note present (CC-1).
- AC: AU fallback is always named, never silent.

### E3-3 — Real functional validation (UniProt + SIFTS + IEDB)
Depends: E3-1 (candidate residues), E3-2 (right structure). Parallel sub-tasks within.

**E3-3.1 — SiftsClient (PDB→UniProt accession + author-numbering residue bridge).**
- Files: `agents/functional/sifts_client.py` (NEW, modeled on `synonym_dictionary/ols_client.py`).
- Steps: GET `ebi.ac.uk/pdbe/api/mappings/uniprot/{pdb}` → per-chain
  `(author_resi → unp_pos)` offset for the chosen chain; cache by pdb_id (immutable).
  **Use SIFTS author numbering (= PyMOL frame); NEVER RCSB `aligned_regions` (label
  frame).**
- Tests (network-gated, real): 2XFB chain A → UniProt **Q1H8W5**, resi 1 → unp **810**
  (**+809 fixture, CC-4 numbering lock**); result non-empty (CC-1).
- AC: the residue bridge is correct on the pinned 2XFB fixture (resi1→810); no UniProt
  xref → named degrade.

**E3-3.2 — UniProtClient (residue features).**
- Files: `agents/functional/uniprot_client.py` (NEW).
- Steps: GET `rest.uniprot.org/uniprotkb/{accession}.json?fields=ft_carbohyd,ft_binding,
  ft_disulfid,ft_act_site,ft_site,ft_domain` → `[(type, unp_start, unp_end, desc)]`;
  cache by accession+release.
- Tests (network-gated, real): Q1H8W5 returns **≥1** feature incl. ≥1 Glycosylation
  (CC-1: assert the feature list non-empty + the known glycosylation present).
- AC: real residue-level features returned for the chosen antigen (non-empty).

**E3-3.3 — IedbClient (known epitopes, bonus, same coord frame).**
- Files: `agents/functional/iedb_client.py` (NEW).
- Steps: GET `query-api.iedb.org/epitope_search?parent_source_antigen_iris=cs.{UNIPROT:<acc>}`
  → epitope spans in UniProt coords; cache by accession+TTL. **Pin the PostgREST
  `cs.{}` array syntax in a test** (a schema change must fail loud, not return `[]`).
- Tests (network-gated): a known antigen with IEDB epitopes returns **≥1** epitope
  span (CC-1); the cs.{} query is pinned; an antigen with none → named absence.
- AC: real IEDB epitope spans returned where they exist; query syntax regression-pinned.

**E3-3.4 — cross-check + wire into the FunctionalValidationStep scan seam.**
- Files: `composition/steps/functional_validation_step.py` (feed annotations via a new
  helper into the existing `_scan_residue_annotations` / `coincidences` seam — output
  contract unchanged).
- Steps: for each candidate `exposed_residue.resi` → `unp_pos` (SIFTS) → test membership
  in UniProt feature spans + IEDB epitope spans → emit
  `"residue N (UniProt M) coincides with <feature/IEDB epitope>"` or
  `"no functional/immunological feature at residue N"`.
- Tests (real, Docker+network-gated, on 2XFB): the step returns a **non-empty**
  `coincidences` OR an explicit per-residue "no feature" list — **never an empty
  `coincidences` with `residue_level_annotation_available: true`** (CC-1 the core
  anti-empty assertion); at least the glycosylation/IEDB coincidence is surfaced when a
  candidate residue overlaps it.
- AC: candidate epitope residues cross-checked against REAL residue-level annotation
  with verified numbering; ≥1 concrete coincidence OR a complete per-residue named
  absence (no silent empty).

**E3-3.5 — SIFTS numbering fixture + degrade-loud paths.**
- Tests: the 2XFB +809 offset fixture (CC-4); no-UniProt-xref / network-down → named
  note + bundle passes through (CC-2); IEDB cs.{} pin.
- AC: a wrong-offset regression is caught by the fixture; every degrade is named.

### E3-8 — Provenance capture
Depends: E3-1, E3-2, E3-3 (the params to record). Parallel after they land.
- Files: the three stage steps + a small `composition/runtime` provenance writer (reuse
  existing provenance wiring if present).
- Steps: record per run — chosen structure id + ranking rationale + assembly-vs-AU;
  PyMOL version + SASA settings; MAFFT version + conservation threshold; the structural
  query (organisms + keywords) + taxon resolution; UniProt/SIFTS/IEDB accessions +
  query dates.
- Tests: a real run's provenance record contains **all** the above keys with non-empty
  values (CC-1: assert each field present + non-empty).
- AC: a run is reproducible from its recorded provenance (every determinism-relevant
  param captured, non-empty).

---

## PHASE 2 — automation / reproducibility (parallel to Phase 1)

### E3-4 — Fully-automated Rhea bring-up (+ push fork main)
Depends: internal order below. Parallel with all of Phase 1.

**E3-4.1 — Fork: HOST=0.0.0.0 + Dockerfile conda → PUSH fork main.**
- Files (rhea fork): `rhea/server/schema.py` (default `host="0.0.0.0"` for
  streamable-http, or honor a `HOST` env), `rhea/server/mcp_server.py`, `rhea/Dockerfile`
  (bake conda for `local` Parsl).
- Steps: fix the container-loopback bind bug; verify the worker is reachable from the
  host when containerized. PUSH to fork main (authorized).
- Tests: a containerized worker answers an MCP `tools/list` from the HOST (the binding
  fix); returns **≥1** tool (`find_tools` at minimum) — CC-1.
- AC: a from-fork image, run with `-p 3001:3001`, answers MCP from the host (non-empty
  tools/list).

**E3-4.2 — apecx-setup Docker `_step_rhea` (build/run/ingest).**
Depends: E3-4.1.
- Files: `apecx-mcp-integration/src/apecx_integration/cli/setup.py:_step_rhea`,
  `infrastructure/{orchestrator.py,containers.py}`; reuse nanobrain `DockerMCPWorker`.
- Steps: `docker build apecx-rhea-server` from the autodiscovered fork; reuse the 3
  sidecar specs (Redis/Postgres/MinIO); run the worker with the orchestrator env
  (`DATABASE_URL→host.docker.internal:5435`, `REDIS_HOST`, `EMBEDDING_URL→Ollama`,
  `PARSL_CONTAINER_BACKEND=local`, `HOST=0.0.0.0`); health-check via
  `DockerMCPWorker.ensure_running()`; ingest the catalog via
  `docker exec … python -m rhea.preprocess.update_tools` (`RHEA_INGEST_ONLY=muscle`).
- Tests (Docker-gated): after the step, the worker's `find_tools("muscle")` surfaces
  **≥1** tool (CC-1: the ingest produced a non-empty catalog); idempotent re-run.
- AC: a single `apecx-setup` step yields a reachable Rhea worker with a non-empty
  ingested catalog.

**E3-4.3 — auto-set RHEA_MCP_URL + daemon-down degrade.**
Depends: E3-4.2.
- Steps: confirm/export `RHEA_MCP_URL=http://localhost:3001/mcp/` for consumers; when
  docker is down → `StepResult("rhea","skipped",…)` with instructions, never raise.
- Tests: with docker down the step returns `skipped` (non-empty detail), chain
  continues; with docker up `RHEA_MCP_URL` resolves for `synthesize_rhea_step`.
- AC: zero user vars required; graceful skip when docker absent.

**E3-4.4 — live `synthesize_rhea_step` integration (closes E2-R live gap).**
Depends: E3-4.3.
- Tests (gated on the live worker from E3-4.2): `synthesize_rhea_step("muscle")` returns
  a **non-empty** Step config with REAL determinism pins (real version, not `@unpinned`;
  container ref present) — CC-1 + the E2-R AC; a synthesized `ToolExecutionStep` runs to
  a concrete non-empty output via `Workflow.run` (CC-2).
- AC: the E2-R live gap is CLOSED — real Galaxy tool → runnable deterministic Step,
  proven end-to-end with non-empty output + real pins.

### E3-7 — PyMOL image build in apecx-setup
Depends: none (but UNBLOCKS E3-1 real-data verification). Parallel any time.
- Files: `apecx-mcp-integration/src/apecx_integration/cli/setup.py` (+ a `_step_pymol`
  or fold into infra): `docker build -t apecx-pymol:3.1.0 docker/pymol/`; idempotent
  (skip if image present); daemon-down → `skipped`.
- Tests (Docker-gated): after the step `docker image inspect apecx-pymol:3.1.0`
  succeeds; a smoke `pymol2` import inside the image returns the pinned version 3.1.0
  (non-empty version string, CC-1).
- AC: `apecx-setup` builds the version-pinned PyMOL image so the structural-reasoning
  real path runs out of the box; re-running E3-1's e2e then yields real SASA on 2XFB
  (closes the real-PyMOL-on-2XFB gap).

---

## PHASE 3 — surface verification

### E3-5 — Verify desktop/headless with a REAL MCP client
Depends: none (verifies shipped streaming). Parallel any time.

**E3-5.1 — minimal real stdio MCP client.**
- Files: `scripts/mcp_stream_client.py` (NEW) using the `mcp` client lib — connects to
  `apecx-mcp` over stdio, calls `run_workflow_streaming` with a `progressToken`,
  collects progress + log notifications.
- Tests: the client connects + lists `run_workflow_streaming` (non-empty tool list).
- AC: a real MCP client process drives the streaming tool over stdio.

**E3-5.2 — end-to-end streaming verification test.**
Depends: E3-5.1.
- Tests (Ollama+Globus+MAFFT-gated): the real client receives **≥6** ordered stage
  notifications (CC-1: non-empty, one per real stage) whose concatenated content is a
  substring of the final headless document; the final result carries the 5-section
  contract.
- AC: a real MCP client demonstrably renders the live per-stage stream end-to-end;
  streamed == headless.

**E3-5.3 — document the desktop contract + mechanism review.**
- Files: `docs/desktop_streaming_contract.md` (NEW).
- Steps: document the client contract (progressToken, notification schema, what to
  render); assess `send_log_message`-for-content vs a cleaner MCP mechanism (streamed
  resource / structured notification); record the recommendation.
- AC: the desktop/headless contract is documented + the transport tradeoff recorded.

---

## PHASE 4 — reliability / conformance hygiene (parallelizable)

- **E3-6 — single-source model resolver + preflight.** Files:
  `agents/_llm_factory.py`, `cli/setup.py`, `composition/composer_config.yml`. One
  `resolve_llm_model()`; a preflight (Ollama `/api/tags`) that FAILS LOUD with
  `ollama pull <model>` before first call. Tests: unpulled model → explicit non-empty
  error naming the pull cmd (CC-1); factory/setup/composer return the SAME default. AC:
  no cryptic 404; one source of truth.
- **E3-9 — conserved-sites caching.** Content-address the `viral_conserved_sites`
  result by `(taxon, protein, aligner)`; reuse G24 `content_hash`. Tests: 2nd run on
  the same key skips MAFFT (timed) + returns the SAME non-empty conservation result
  (CC-1/CC-4). AC: re-runs skip the ~6-min align; identical conserved regions.
- **E3-10 — test-isolation fix.** Find the test leaking `SearchClient` mock/env; add
  fixture cleanup. Test: `test_globus_search.py` + the synthesizer suite run TOGETHER =
  green (the 4 failures gone). AC: no cross-file contamination.
- **E3-11 — EnvelopeStepConfig conformance.** `extra='forbid'` + `COMPONENT_TYPE`.
  Test: a YAML typo on that config FAILS LOUD (non-empty error). AC: matches every
  other step config.
- **E3-12 — Rhea G130 doc cross-ref.** Add G130 to
  `apecx-mcp-integration/docs/nanobrain_capability_gaps.md` + the
  `nanobrain-agents-tools`/`nanobrain-lightweight` SKILLs. AC: G130 discoverable from
  the canonical gaps doc.
- **E3-13 (optional) — multi-structure structural reasoning.** Analyze top-N ranked
  structures. Test: a CHIKV query analyzes ≥2 structures, each with non-empty residue
  classifications (CC-1). AC: epitope surface corroborated across ≥2 structures.
- **E3-14 (optional) — model quality tier.** Document `mistral-nemo` as the quality
  tier; a gated comparison test records per-stage quality delta vs nemotron-3-nano. AC:
  the tradeoff is measured + documented.

---

## Suggested execution order (when implementation is authorized)
1. **E3-7** (PyMOL image) + **E3-4.1** (fork fix) first — they unblock real-data
   verification for E3-1 and E3-4 respectively; both small + independent.
2. **E3-2** (query precision) — the relevance foundation; everything structural depends
   on picking the right structure.
3. **E3-1** (assembly SASA) — verify with the E3-7 image.
4. **E3-3** (functional) — the three clients can be built in parallel; E3-3.1 SIFTS is
   the critical bridge (build + fixture first).
5. **E3-4.2→4.4** (Rhea setup + live test) — closes the E2-R live gap.
6. **E3-8** (provenance) once 1–4 land.
7. **E3-5** (MCP client) + **Phase 4** hygiene — anytime, parallel.

Each task = its own commit (per-repo), real-data verified per its AC + CC-1..CC-5
before the next. Multi-hour real-data runs (e2e, ingest, image build) are expected and
fine.

---

# 2026-06-14 UPDATE — implementation status, desktop/headless verdict, new + leftover tasks

Plan refresh requested after the v2.1 implementation pass. Reflects what shipped, the
answer to "does the desktop/headless split actually work", and the new tasks surfaced by
hardening (the "anything else that warrants improvement"). No code is written here — this
is the planning artifact only. Cross-repo verification baseline at time of writing:
nanobrain 1254 unit + apecx 1644 unit + evidence 14/14 integration, all green.

## A. v2.1 status — ALL E3-* SHIPPED (apecx branch `epitope-evidence-workflow`, unpushed)

| Task | What | Commit(s) |
|------|------|-----------|
| E3-1 | Biological-assembly SASA + mmCIF large-assembly fallback | `af900e7` |
| E3-2 | Taxon-precise structural Globus query | (earlier; see v2 plan) |
| E3-3 | Real functional validation (UniProt + SIFTS + IEDB) | (earlier) + `e3f72ce` (SIFTS null-author-edge recovery) |
| E3-4 | Fully-automated Rhea bring-up + fork-main push | (earlier) |
| E3-5 | Desktop/headless verified over a REAL stdio MCP client | `1b5eeb7` + `34a8acc` (MCP logging capability) |
| E3-6 | Single LLM-model source of truth + loud preflight | `695df98` |
| E3-7 | PyMOL image in apecx-setup | (earlier) |
| E3-8 | Per-run provenance capture | `0c37150` |
| E3-9 | Content-addressed conserved-sites MAFFT cache | `9a2d60d` |
| E3-13 | Multi-structure corroboration (top-N) + chain-pinning | `d548c26`, `3b17cba` |
| E3-14 | Model tiers documented (nemotron-3-nano vs mistral-nemo) | `dec71f1` |
| E3-R-followup | Rhea tool EXECUTION closed (synthesized muscle runs e2e) | `47fa617` |

**Bug chain found by the multi-taxon + diverse-input probes (all shipped):** execution_timeout
on slow legs (`55c6e86`), length-cluster protein selection (`dd363cc`), word-boundary protein
match — stop aligning the wrong protein (`3f9062f`), arbitrary-virus → BV-BRC taxon resolver
(`5d63f29`), `_VIRUS_RE` never matched singular "virus" (`7bc063b`), SubworkflowStep fast
inner-failure detection (`aa7c9e1`, nanobrain `d397915`).

**Reliability hardening shipped THIS session (beyond v2.1 scope — the silent-failure sweep):**

| Item | Severity | Commit |
|------|----------|--------|
| nanobrain `Workflow.run` concurrent-instance cross-contamination (data corruption) | **Critical** | nanobrain `5f93696` |
| HITL design-gate bypass — fail-closed, scope-bound approval (was: any string opened it) | **High (security)** | `659fac0` + `f00a003` |
| Unbounded RunStore + handle store (long-lived-server OOM) | High | `b17f01d` |
| SIFTS null-author-edge — functional validation false-negative | High | `e3f72ce` |
| Query markdown-injection into deterministic sections | Medium | `9b2e909` |
| Scope caveat when no virus resolves (non-viral query read as authoritative) | Medium | `5607d9d` |
| VIOLIN+BV-BRC agent — 3 silent error-swallows → degrade-loud | Medium | `6a94659` |
| MemoryStore corrupt-file skips → degrade-loud | Low | `c90072b` |
| Streamed e2e test order-independence (cache clear) | Low (test) | `f210743` |

Distilled lessons persisted: workspace `CLAUDE.md` (long-lived-service concurrent/repeated
testing rule), `nanobrain-workflow-authoring` skill (concurrent-run silent-failure shape),
3 memory files. nanobrain `CLAUDE.md` records the `Workflow.run` serialization fix.

## B. Desktop vs headless split — VERDICT: works as intended

Three tools form the split: `run_workflow` (headless one-shot), `run_workflow_streamed`
(callback primitive), `run_workflow_streaming` (MCP-notification adapter, `ctx`-gated). With
`ctx=None` the streaming tool runs headless unchanged; with a `ctx` it emits, per completed
stage, a `report_progress` + a structured `send_log_message` carrying the full stage report.
Streaming is observability-only: notification failures are caught + logged and NEVER change
the returned `WorkflowResult` (headless == desktop result, by construction).

**Proven, not assumed:** `tests/integration/test_mcp_stream_client.py` launches `apecx-mcp`
as a subprocess, does a REAL stdio MCP handshake (incl. `set_logging_level("info")` — the
Claude-Desktop path; the `34a8acc` fix advertises the MCP `logging` capability so this no
longer tears the session down), registers real log + progress handlers, and asserts ≥6 stage
notifications arrive IN ORDER and that streamed == headless reasoning-trace. Unit tests cover
the mock-ctx emit path, the headless passthrough, and server registration.

**Two caveats (documented, not blocking):**
- **B-1 (benign):** a cached workflow re-run with byte-IDENTICAL consecutive input does NOT
  re-execute (DataUnitChangeTrigger deterministic-skip; the prior output is the correct
  deterministic answer) → a desktop re-query of the exact same params streams NOTHING then
  returns the cached doc. Correct behavior; only the UX (no progress shown on an instant
  cached answer) is slightly surprising. See task **E4-7**.
- **B-2 (test quality):** under random test ordering (`pytest-randomly`),
  `test_eo_primitives.py` + `test_mcp_stream_client.py` run together can raise
  `TypeError: _test_catalog_for.<locals>._loader() takes 0 positional arguments but 1 was
  given` — a cross-file test-isolation leak (the process-wide `_workflow_cache` / a catalog
  loader fixture). The full 1644-suite passes (order-dependent), so it is latent, not a
  product bug. See task **E4-4**.

## C. New + leftover tasks (E4-*) — "anything else that warrants improvement"

Priority key: **P1** = real reliability/security gap worth doing; **P2** = quality/UX
improvement with a tradeoff; **P3** = low-severity / optional. Each carries data-based
acceptance (CC-1..CC-5 still apply: no empty responses, decide from output VALUES, real-data
integration test for "done").

- **E4-1 — HITL design-approval: persistence + human-only approval surface. (P1, security)**
  The fail-closed gate (`659fac0`) closes the any-string bypass, but two boundaries remain
  (documented in `design_approval_store.py`): (a) the store is in-process/session-scoped —
  a server restart drops issued/approved tokens (a caller must re-request); (b) `approve_design`
  is an LLM-callable MCP tool, so a fully-autonomous LLM can self-approve — genuine HITL only
  holds under a human-operated client. **Sub-tasks:** E4-1a durable backend for
  `DesignApprovalStore` (mirror the RunStore/HandleStore swap-in seam); E4-1b a human-only /
  auth-gated approval path (cross-cutting — applies equally to the control-plane
  `approve`/`reject` tools; coordinate, don't fork). **AC:** a token approved on server A is
  honored after restart (E4-1a); an LLM identity cannot approve its own request (E4-1b),
  asserted by a test that the autonomous path is rejected. **Dep:** E4-1b needs an auth model
  decision (currently no auth layer — `decided_by` defaults to `api_user`).

- **E4-2 — `Workflow.run` concurrency: throughput vs the current serialization. (P2, framework)**
  `5f93696` made concurrent same-instance runs CORRECT by serializing them (per-instance
  lock). Distinct workflows still parallelize; same-workflow concurrent requests now QUEUE.
  For higher throughput, build a DISTINCT workflow instance per concurrent run (no shared data
  units → true parallelism) instead of caching one. **Tradeoff:** build cost per call + shared
  sub-resources (LLM client, SIFTS cache) need their own concurrency review. **AC:** two
  concurrent distinct-virus queries complete in ~max(t1,t2), not t1+t2, each isolated +
  correct (extend the existing nanobrain concurrent-isolation test). **Decision needed:** the
  user previously chose "keep edge-hardening" over this; revisit only if MCP-server throughput
  becomes a measured need. Currently correct-but-serialized is acceptable.

- **E4-3 — Structure selection prefers a UniProt-xref-bearing PDB. (P2, functional coverage)**
  Structural reasoning ranks by epitope-relevance; if the top PDB lacks a SIFTS UniProt xref
  (e.g. SARS-2 spike 8F2V, genuinely unmapped) functional validation degrades loud to "no
  residue annotation" even though xref-bearing spike structures exist. **Tradeoff:** coupling
  selection to xref-availability could pick a less-epitope-relevant structure, and costs a
  SIFTS call per candidate. **Proposed:** among the top-K already-ranked candidates, prefer one
  WITH an xref ONLY when epitope-relevance is within a small margin; never sacrifice a clearly
  better structure. **AC:** for a virus where the top-ranked PDB has no xref but a near-tied one
  does, functional validation now returns residue-level annotation; provenance records both the
  relevance margin and the xref-preference decision. CC-1: the annotation is real + non-empty.

- **E4-4 — Test-isolation flake under random ordering (B-2). (P3, test quality)**
  `_test_catalog_for._loader` (0-arg) is invoked with 1 arg under some `pytest-randomly`
  orderings when `test_eo_primitives` + `test_mcp_stream_client` share the process-wide
  `_workflow_cache`. **Proposed:** an autouse fixture that calls `_clear_workflow_cache()` (+
  any catalog-loader reset) around these tests; OR make the test catalog loader signature
  match the framework's call. **AC:** the two files run together under forced random seeds stay
  green across ≥20 seeds. (Note: distinct from E3-10's already-fixed SearchClient leak.)

- **E4-5 — PDB/EMDB as first-class harmonized sources (Track A–C of the action plan). (P1,
  but EXTERNALLY GATED)** Make PDB & EMDB canonical-IRI-matchable harmonized indices (today
  the evidence workflow's structural leg uses a freetext Globus search, which works but is not
  harmonized). **Blocked on an OPS action (X1): two new Globus Search DEST indices must be
  created with index-admin privileges — not code.** Phase-0 read-only probe of aggregate index
  `e74bf12a` is the un-gated first step and sizes the rest (A/B/C scope branch). See
  `viral_epitope_evidence_workflow_action_plan.md` Tracks A–C + the worktree plan
  `~/.claude/plans/okay-which-worktree-has-enumerated-marshmallow.md`. **AC (Phase 0):** a saved
  real PDB + EMDB record fixture from a Globus index with the PDB/EMDB discriminator field
  named (or its absence proven). **Do not start A–C code until X1 + Phase-0 = A or B, recorded.**

- **E4-6 — External-DB title/field injection into the Sources section. (P3, low)** Publication
  / genome / VIOLIN titles are interpolated raw into the deterministic Sources list (RAG
  snippets already collapse newlines; the others don't). A malformed external title with a
  newline + `##` could inject a stray header — but external DBs are curated (low probability)
  and it lands in the Sources section past the contract-critical headers (cosmetic). **Proposed:**
  collapse newlines (only) in rendered external strings — NOT the full `_sanitize_inline` (which
  would over-mangle legitimate long / `#`-bearing titles). **AC:** a fixture publication with a
  newline+`##` title yields no extra `##` heading; legitimate titles render unchanged.

- **E4-7 — Desktop streaming on an identical cached re-query (B-1). (P3, UX)** A desktop
  re-query of byte-identical params streams nothing (deterministic-skip) then returns the cached
  doc instantly. Options: (a) leave as-is + document (the answer is correct + instant — arguably
  fine); (b) on a cache-hit-with-no-execution, emit ONE synthetic "served from cache (no
  re-computation)" progress notification so the desktop pane isn't silent. **AC (if b):** an
  identical re-query emits exactly one cache-hit notification and the same final doc. Lowest
  priority — current behavior is correct, only the progress UX is bare.

- **E4-8 — MemoryStore + other best-effort skips: confirm coverage. (P3)** `c90072b` made the
  two MemoryStore corrupt-file skips loud. A follow pass over the remaining bare-`except` sites
  (composer, control_plane routes) confirmed they re-raise / log / are best-effort error-message
  rendering (legitimate). No further action unless a new live-path silent swallow is found.

### Suggested order for the new work (when authorized)
1. **E4-4** (test flake) — cheap, removes CI noise, no design decision.
2. **E4-1a** (HITL durable backend) — P1 security, self-contained (mirrors existing store
   seams). E4-1b (auth) waits on the cross-cutting auth-model decision.
3. **E4-5 Phase-0 probe** — un-gated, read-only; sizes the PDB/EMDB track (the rest waits on
   ops X1).
4. **E4-3** (structure xref preference) — P2, needs the relevance-margin design.
5. **E4-6 / E4-7 / E4-2** — P2/P3, do opportunistically or on explicit request; E4-2 only if
   throughput is measured to matter.
