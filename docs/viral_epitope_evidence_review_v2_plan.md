# `viral_epitope_analysis` v2 — Multi-Stage Reasoning + Dynamic Tooling Plan

Status: DRAFT 2026-06-13. Branch home: `epitope-evidence-workflow` (wt-eo-mvp),
nanobrain changes on their own branch. Cite task IDs (E2-*) in commits/PRs.

This plan responds to the directive of 2026-06-13: fix the model default + the
"empty PDB/EMDB results" issue (both DONE this session), then redesign the
workflow into a documented multi-stage reasoning pipeline with a fixed output
contract, add headless PyMOL structural reasoning, make Rhea/Galaxy tooling
dynamic-and-deterministic, and address the desktop-vs-headless surface split.

---

## 0. Already fixed this session (shipped, real-data verified)

| Commit | Fix | Verification |
|---|---|---|
| `2941b2c` (apecx) | **Model default → `nemotron-3-nano:4b`** (the installed model). Was `mistral-small:latest`, never pulled. | evidence e2e 7/7 on nemotron-3-nano (real LLM + Globus) |
| `<datacite>` (apecx) | **DataCite render fix** — titles/descriptions/subjects now surfaced (were `(untitled)` on every record). 3 render sites + shared extractor. | `harmonized_search("chikungunya antibody", index="pdb")` returns labeled antibody-antigen complexes; unit 19/19 |
| `9fe4c30` (nanobrain) + `a242166` (apecx) | **Approval-gated fan-in** (prior task): AllDataReceived value-comparison re-arm + deposit-key fix. | regression 363/0; e2e 7/7 |

These close observation #1 (symptom) and the "empty list" report (root cause).
The **core** issues behind them are tracked below (E2-A model resolver, and the
fact that the empty-list was a render bug, now fixed).

---

## 1. Findings that size the work

### 1.1 Model-default divergence (root cause of observation #1)
Three subsystems carried **three different default model names**:
`build_chat_llm` → `mistral-small:latest`; `setup.py:_ollama_model()` (what the
installer pulls) → `mistral-nemo:latest`; `composer_config.yml` → `mistral-small`.
A fresh install pulls one model and the runtime asks for another → cryptic Ollama
404 at first call. The symptom is fixed (default now points at an installed
model); the **core fix** is a single source-of-truth resolver + a loud preflight
(E2-A).

### 1.2 The "empty PDB/EMDB results" report was a render bug, not empty retrieval
Every record in the aggregate index `e74bf12a` is DataCite-shaped (title at
`titles[0].title`, abstract at `descriptions[0].description`). Three renderers
read the flat `content.get("title")` → `None` for **every** record. Retrieval was
correct and relevance-ranked; the render dropped all meaning to `(untitled)`.
Fixed this session. **This silently degraded the entire Globus synthesis branch
for all record types, not just structures.**

### 1.3 How PDB/EMDB harmonized search actually works (verified)
α design: a freetext `q=<term>` query on `e74bf12a` with a `publisher.name`
`match_any` filter (`RCSB PDB` / `Electron Microscopy Data Bank`). Verified
relevance: `chikungunya` vs `neuraminidase` share **0 of 50** subjects;
`chikungunya antibody` → `total=3522`, top hits are CHIKV-Fab complexes. It does
**not** use `subjects.valueUri` (empty for PDB/EMDB) or taxonomy resolution —
correct, those are taxonomy concepts. **It returns relevant results; it never
returned empty-on-every-request.** Residual hardening: query construction is bare
freetext — add taxon-aware query expansion (E2-D5) and a relevance floor.

### 1.4 Current workflow vs the 6-stage spec — the gap is architectural
Current `viral_epitope_analysis` = `normalize → assemble → structural →
review → gate → envelope`: a retrieve→one-LLM-synthesize→gate pipeline with **one**
LLM call emitting **one** terminal Markdown blob. Measured against the spec:
**~1 of 6 reasoning stages** (and only implicitly), **0 of 5 mandated output
sections**, **no sequence-level or functional-level reasoning, no PyMOL, no
data-readiness report, no stage streaming.** Strengths: reliability engineering
(degrade-loud everywhere), rigorous citation grounding, clean approval gate,
strong nanobrain conformance. The redesign changes the **shape**, not the polish.

### 1.5 Rhea is rigid at exactly three seams
(a) **No "tool name → synthesized Step" function** — `RheaMCPDiscovery` stops at a
UTD *dict*; a human hand-places it into a `ToolExecutionStep` and hand-wires the
workflow. (b) **Two disjoint execution paths** — JSON tools via `ToolExecutionStep`
+ `RheaAdapter`; the *majority* file-input Galaxy tools via a separate,
non-UTD-driven `RheaFileToolStep`. (c) **Determinism pins dropped at the MCP
wire** — Rhea's `Tool` schema carries `version`/`requirements`/`containers`, but
the MCP `tools/list` payload omits them, so `RheaMCPDiscovery` hardcodes
`version=1.0.0`, `determinism=R3`, `side_effects=network` for every tool and never
sets a container digest. Plus runtime conda-env build (version floors, not pins),
no seed surface, and stochastic `find_tools` RAG selection.

### 1.6 agentic-pymol — MIT, but its GUI-socket delivery is the wrong shape
It's an MCP server that talks over a TCP socket to a **manually-armed, GUI
desktop PyMOL plugin** — incompatible with deterministic automation. It works with
**open-source PyMOL** (MIT license, no incentive license). Its typed tools cover
geometry/iterate/alter/sequence/align/render; **SASA and contact maps — the two
epitope-critical analyses — are absent** and must be added. Recommended: **vendor
its MIT tool code as reference**, run open-source PyMOL **headless** (`pymol2.PyMOL()`
in-process or `pymol -cq` batch), use **ray-traced render (CPU, deterministic),
never screenshot**, pin camera (`get_view`/`set_view`), pin `dot_solvent`/
`dot_density` for SASA, pin the PyMOL version.

### 1.7 Desktop-vs-headless: there is no split today
One invocation path: MCP tool `run_workflow` on a FastMCP **stdio** server,
strictly **one-shot** (`await_cascade=True` → single envelope). No streaming of
intermediate stage reports. `inspect_run` is pull-only, status/timing, no
reasoning content. nanobrain's `subscribe_to_step_events` (G37) is the natural
seam for streaming per-stage reports to a desktop UI.

---

## 2. Product assessment (strengths / weaknesses / reliability / conformance)

**Strengths.** Reliability-first: every degrade path is loud (branch failure →
warning+`[]`; structural no-hit/outage → named note; LLM-gate failure →
evidence-preserving fallback). Citation grounding validates every emitted token
against rendered inputs (rejects hallucinated IDs). G127 trap avoided
(success decided from output value, not run status). Approval-as-data gate.

**Weaknesses.** Single-LLM-call architecture: a degraded model degrades the whole
product with no independent functional/structural cross-check. No staged
reasoning, no sequence/functional stages, no structural reasoning (only
retrieval), no data-readiness report, no fixed output contract, no streaming.
Pre-existing **test-isolation bug**: 4 globus tests fail when run alongside the
synthesizer suite (a leaked `SearchClient` mock/env), pass in isolation — masks
real failures (E2-Q3).

**Reliability verdict.** Defensive reliability is strong; *epistemic* reliability
is weak — there's no second opinion on the one LLM pass. The multi-stage redesign
(each stage independently checkable) is itself the main reliability upgrade.

**Nanobrain conformance.** Strong: all steps `from_config` + `process()`-only,
`extra='forbid'` (except `EnvelopeStepConfig` — E2-Q4 nit), `config_version: 2`
auto_transfer, G118 fan-in pattern, no cycles. The redesign stays native; any
framework gap (streaming surface, Rhea synthesis) ships as a nanobrain capability
+ skill + regression test.

---

## 3. Target architecture

### 3.1 Six documented reasoning stages
Each stage is a `BaseStep` that (a) does its analysis, (b) emits a **documented
sub-report** (Markdown fragment + structured data), (c) publishes a `StepEvent`
so the surface can stream it. The envelope concatenates sub-reports into the final
contract (§3.2).

```
0. data_readiness   — query the indices, report what's available + coverage gaps
1. sequence_reason  — conserved-sites / MSA analysis (compose viral_conserved_sites)
2. structural_reason— PyMOL: map sequence/conservation onto 3D, SASA, epitope surface
3. functional_valid — cross-check candidate epitopes vs functional annotation
4. evidence_reason  — grounded synthesis over literature + DB evidence (today's review)
5. crossdb_integrate— reconcile sequence↔structure↔function↔literature into one view
   → gate(approval) → envelope(output contract)
```

- **Stage 0 (data readiness)** is trivially seeded from the assembly bundle counts
  already computed; emit a "what's in the indices for this query" summary +
  named coverage gaps (degrade-loud carried forward).
- **Stage 1 (sequence)** composes the existing `viral_conserved_sites` workflow
  (BV-BRC→MAFFT→conservation), today a sibling. Requires lightweight subworkflow
  nesting (nanobrain gap F1 / E2-F1) OR flattening its steps into this builder.
- **Stage 2 (structural)** is the new PyMOL step (§3.3), consuming stage-1
  conserved positions + stage-0 structures.
- **Stages 3–5** split today's single synthesis into discrete, individually
  reported reasoning passes, each with its own grounded-citation gate.

### 3.2 Output contract (mandated)
Terminal Markdown MUST carry, in order:
`# Answer` · `## Cross-data reasoning` · `## Integrated insight` ·
`## Sources and evidence` (deterministic, citation-bearing — built from the
bundle, not the LLM) · `## Follow-up questions`. Lever: rewrite
`synthesis_config.yml:system_prompt` to require the first three; build Sources and
Follow-ups deterministically. This is the highest value-per-effort change (0/5 →
5/5) and is independently shippable before the full stage redesign.

### 3.3 Structural reasoning via headless PyMOL (new tooling)
A `StructuralReasoningStep` (`BaseStep`, Process/Parsl-executor friendly) that, per
candidate structure: fetch coords → align variants (`cealign` for low identity) →
map conservation/epitope scores to b-factor → per-residue SASA (`get_area`,
pinned `dot_solvent`/`dot_density`) to classify exposed vs buried → emit a
contact map (numpy over coords) → ray-render a colored epitope figure (deterministic
CPU ray, pinned camera). Outputs: SASA table, contact map, RMSD, PNG artifact,
all as DataUnits. Built over open-source PyMOL headless; vendor agentic-pymol's
MIT tool code as reference. PyMOL version pinned + recorded (HPC-determinism
contract).

### 3.4 Stage streaming — the desktop-vs-headless surface
- **Headless backend**: `run_workflow` stays one-shot; envelope concatenates all
  stage sub-reports (current consumers unaffected).
- **Desktop application**: a new streaming entry (`run_workflow_streamed`) that
  subscribes via nanobrain `subscribe_to_step_events` (G37) and pushes each
  stage's sub-report as it completes. The desktop UI renders stages live; the
  backend writes the same sub-reports durably. One workflow, two surfaces — the
  split is in the *invocation/transport*, not the workflow.

### 3.5 Rhea → nanobrain dynamic tool synthesis (deterministic)
Goal: `WorkflowBuilder.add_rhea_tool(name_or_query, inputs, ...)` → a
deterministic nanobrain Step. Building blocks mostly exist (`RheaMCPDiscovery`,
`ToolExecutionStep`, `RheaAdapter`, `RheaFileToolStep`, UTD determinism fields,
`DockerMCPWorker`, G24 content-hash). Net-new:
1. **Tool→Step synthesizer** — `find_tools` to surface → read `inputSchema` →
   branch file-vs-JSON → emit `RheaFileToolStep` or `ToolExecutionStep`+UTD →
   register + wire links in the builder.
2. **Unify the two tool-shape paths** under one UTD-driven entry.
3. **Determinism wire (Rhea-side)** — surface `version`/`requirements`/`containers`/
   `version_command` into the MCP `tools/list` payload (`annotations`); have
   `RheaMCPDiscovery` read them into the UTD `descriptor_id` version,
   `provenance_pin.container_image_digest`, and a real `DeterminismClass`.
4. **Content-addressed file staging** (wire G24 `content_hash` into the Rhea
   ProxyStore path) + a seed surface for inherently-stochastic tools.

### 3.6 Single-source model resolver + preflight (E2-A)
One `resolve_llm_model()` read by `build_chat_llm`, `setup.py`, and composer; a
preflight that on first use checks the model is pulled (Ollama `/api/tags`) and
FAILS LOUD with `ollama pull <model>` guidance instead of a runtime 404.

---

## 4. Task dependency tree

```
DONE: model-default symptom (2941b2c), DataCite render (datacite commit), fan-in (9fe4c30/a242166)

E2-A model resolver+preflight ──────────────┐ (independent, small)
E2-B output contract (Answer/.../Follow-ups)─┤ (independent, high value, ship first)
                                             │
E2-C stage scaffolding (StepEvent sub-reports)
   ├─ E2-C0 data_readiness step
   ├─ E2-C1 sequence stage  ◄── E2-F1 (nest viral_conserved_sites) OR flatten
   ├─ E2-C2 structural reasoning ◄── E2-P (PyMOL tooling) , E2-C1 (conserved positions)
   ├─ E2-C3 functional validation
   ├─ E2-C4/5 evidence + cross-db (split today's synthesis)
   └─ E2-S streaming surface ◄── G37 subscribe_to_step_events

E2-P PyMOL headless tooling (vendor agentic-pymol MIT; add SASA+contacts)
E2-F1 nanobrain lightweight subworkflow nesting (framework) ◄── G115/G117

E2-R Rhea dynamic tool→Step
   ├─ E2-R1 tool→Step synthesizer (apecx/nanobrain)
   ├─ E2-R2 unify file-vs-JSON UTD path (nanobrain)
   ├─ E2-R3 determinism wire (RHEA-SIDE change) ◄── escalate: separate repo
   └─ E2-R4 content-addressed staging + seed surface

E2-Q quality: Q3 test-isolation bug, Q4 EnvelopeStepConfig extra=forbid,
   Q5 multi-trial e2e harness (N runs, real LLM+Globus), D5 taxon-aware query expansion
```

**Shortest path to visible value:** E2-B (output contract) → E2-C0 (data readiness).
**Critical path to full spec:** E2-P → E2-C2 (structural reasoning) and E2-F1 → E2-C1.

---

## 5. Tracks and tasks

### Track A — Reliability/plumbing (small, independent)
- **E2-A** Single-source `resolve_llm_model()` + loud model preflight. Reconcile
  factory/setup/composer. Test: preflight FAILS LOUD on an unpulled model name.
- **E2-Q3** Fix the test-isolation leak (find the test that leaks `SearchClient`
  mock/env; add fixture cleanup). Test: `pytest tests/unit/test_globus_search.py
  tests/unit/<synth suite>` together = green.
- **E2-Q4** `EnvelopeStepConfig` → `extra='forbid'` + `COMPONENT_TYPE`.
- **E2-Q5** Multi-trial e2e harness: run the evidence e2e N≥5 times across
  {nemotron-3-nano, mistral-nemo} × {CHIKV, DENV, influenza, SARS-CoV-2} and assert
  stable structure + non-empty grounded evidence each run.
- **E2-D5** Taxon-aware structural query expansion (resolve query→species synonyms
  before the freetext `q=`), + a relevance floor that names low-confidence hits.

### Track B — Output contract (high value, independent)
- **E2-B** Rewrite `synthesis_config.yml:system_prompt` to mandate `Answer` /
  `Cross-data reasoning` / `Integrated insight`; build `Sources and evidence`
  (citation-bearing, deterministic from the bundle) + `Follow-up questions`
  (deterministic templates seeded by the query + gaps). Test: the markdown carries
  all 5 sections on a real run; Sources lists every cited token.

### Track C — Multi-stage reasoning (the redesign)
- **E2-C scaffolding** Stage base mixin: each step emits a `StepEvent` carrying its
  Markdown sub-report + structured payload; envelope concatenates in spec order.
- **E2-C0** `DataReadinessStep` — index coverage summary + named gaps.
- **E2-C1** Sequence stage — compose `viral_conserved_sites` (needs E2-F1) or
  flatten its 3 steps. Output: conserved positions + per-residue conservation.
- **E2-C2** `StructuralReasoningStep` (needs E2-P): map conservation→structure,
  SASA, contacts, epitope figure.
- **E2-C3** `FunctionalValidationStep` — cross-check epitope candidates vs
  functional annotation (VIOLIN/BV-BRC/UniProt features).
- **E2-C4/5** Split synthesis into evidence-reasoning + cross-db-integration, each
  grounded-citation-gated.
- **E2-S** Streaming surface (`run_workflow_streamed` via G37); headless stays one-shot.

### Track P — PyMOL structural tooling
- **E2-P1** Vendor agentic-pymol MIT tool wrappers; headless `pymol2`/`pymol -cq`
  boot; pin version + camera + SASA settings.
- **E2-P2** Add SASA (`get_area`) per-residue exposed/buried classification.
- **E2-P3** Add contact-map (numpy over `get_coords`).
- **E2-P4** Conservation→b-factor mapping + ray-rendered epitope figure (deterministic).
  Tests: same input → byte-stable SASA table + RMSD; figure render is reproducible.

### Track F — nanobrain framework
- **E2-F1** Lightweight-native subworkflow nesting (so a builder can run another
  builder's `Workflow.run`, honoring G115/G117). Ships with skill + regression test.
- **E2-F2** (optional) StepEvent → external stream adapter helper for desktop surfaces.

### Track R — Rhea/Galaxy dynamic tooling
- **E2-R1** `add_rhea_tool` synthesizer (tool name → UTD → Step → wired in builder).
- **E2-R2** Unify file-vs-JSON under one UTD-driven entry.
- **E2-R3** **(Rhea-repo change — escalate to user before touching)** surface
  version/requirements/container digest into MCP `tools/list`; read into UTD.
- **E2-R4** Content-addressed staging (G24) + seed surface; classify determinism
  honestly instead of blanket R3/network.

---

## 6. Gated tests (rooted in real data)

| Test | Gate | Real-data assertion |
|---|---|---|
| `test_model_preflight_fails_loud` | none | unpulled model name → explicit error naming `ollama pull` |
| `test_output_contract_sections_present` | Ollama | real run markdown has all 5 mandated sections; Sources lists every cited token |
| `test_structural_search_relevance` | Globus | `chikungunya` vs `neuraminidase` share 0/50 subjects; hits carry real titles (regression-pins the DataCite fix) |
| `test_pymol_sasa_deterministic` | PyMOL | same structure → byte-identical per-residue SASA table across 2 runs |
| `test_pymol_render_reproducible` | PyMOL | pinned camera → identical PNG bytes across 2 runs |
| `test_sequence_to_structure_mapping` | BV-BRC+PyMOL | conserved positions map to real residue numbers on a real PDB; exposed-vs-buried classified |
| `test_stage_events_streamed` | Ollama | each of the 6 stages emits exactly one StepEvent sub-report; order = spec |
| `test_rhea_tool_to_step_synthesis` | Rhea worker | `add_rhea_tool(<known galaxy tool>)` → a runnable Step producing the expected output shape |
| `test_rhea_determinism_pins_surfaced` | Rhea worker | discovered UTD carries the real tool version + container digest (not 1.0.0/R3) |
| `test_multitrial_e2e_stable` | Ollama+Globus | N≥5 runs × {2 models} × {4 taxa}: every run has the 5 sections + ≥1 grounded structural hit |

No mock-only completion: each mocked unit has a matching real-dependency
integration test (workspace parity rule).

---

## 7. Data-based acceptance criteria

| # | Criterion | Verified by |
|---|---|---|
| AC-A | A configured-but-unpulled model FAILS LOUD with pull guidance, never a runtime 404. | `test_model_preflight_fails_loud` |
| AC-B | Output carries Answer / Cross-data reasoning / Integrated insight / Sources / Follow-ups on every real run; Sources is citation-complete. | `test_output_contract_sections_present` |
| AC-C | All 6 reasoning stages execute, each emits a documented sub-report, in spec order; data-readiness names coverage gaps. | `test_stage_events_streamed` |
| AC-P | Structural reasoning maps conserved positions to real 3D residues, classifies SASA, and renders a reproducible epitope figure deterministically. | `test_sequence_to_structure_mapping`, `test_pymol_*` |
| AC-R | The builder synthesizes a deterministic nanobrain Step from a Galaxy tool name, with real version/container pins. | `test_rhea_tool_to_step_synthesis`, `test_rhea_determinism_pins_surfaced` |
| AC-REL | N≥5 multi-taxon, multi-model e2e trials all produce the full contract + ≥1 grounded structural hit (no empty/untitled). | `test_multitrial_e2e_stable` |
| AC-SURF | Headless `run_workflow` returns the concatenated contract one-shot; desktop `run_workflow_streamed` pushes each stage live — same workflow. | `test_stage_events_streamed` + a streaming integration test |

---

## 8. Open decisions / risks (need a call before building)

1. **Sequence stage: nest vs flatten.** E2-F1 (nanobrain lightweight nesting) is
   the clean path but a framework change; flattening duplicates the conserved-sites
   pipeline. Recommend E2-F1.
2. **nemotron-3-nano:4b quality.** It passed the e2e contract, but a 4B model will
   produce weaker reasoning across 6 stages with 5 citation gates. Recommend it as
   the *default* (works out-of-box) but document mistral-nemo as the quality tier;
   E2-Q5 measures the gap per stage.
3. **Rhea determinism wire (E2-R3) touches the Rhea repo** — a separate codebase.
   Escalate before editing; until then UTDs carry honest "unpinned" determinism.
4. **PyMOL dependency.** Open-source PyMOL must be installable in the backend env
   (and HPC). Confirm it's acceptable as a backend dependency before E2-P.
5. **Streaming transport.** Desktop surface needs a transport for StepEvents (MCP
   notifications? SSE? websocket?). Decide the desktop app's channel before E2-S.

---

# v2.1 — Follow-up plan (2026-06-13)

## v2 status: SHIPPED (recap)
The 6-stage redesign shipped on `epitope-evidence-workflow` (~14 commits) + 3 stacked
nanobrain branches + 1 Rhea branch; nothing pushed. The workflow is now an 11-step
pipeline (`normalize → assemble → data_readiness → structural → sequence → merge →
reasoning(PyMOL) → functional → review → gate → envelope`) with: all 6 reasoning
stages, the deterministically-guaranteed 5-section output contract (+ degrade-path
header sanitize), the `SubworkflowStep.inner_workflow_builder` nesting capability,
containerized headless PyMOL SASA with surface-antigen ranking, G37 streaming
surface, and the Rhea tool→Step synthesizer + determinism wire (unit-proven). 110
unit tests green; real-data e2e verified. Reliability fixes: model-default 3-way
divergence, DataCite render, fan-in re-arm, degrade-path. Two real-data gaps remain
(blocked externally): Rhea live (no worker), real-PyMOL-on-2XFB (docker daemon down).

This v2.1 plan incorporates the four new directives (accessibility, query precision,
real functional validation, Rhea automation), the desktop/headless verification, and
all carried-over leftovers. **All findings below are real-data backed (investigated
2026-06-13).** No code written yet.

## E3-1 — Biological-assembly SASA (accessibility) [scientific correctness]
**Problem.** SASA is computed over the deposited **asymmetric unit**, not the
**biological assembly**. An oligomer-interface residue reads as "exposed" when it is
actually buried in the functional oligomer — a real epitope-accessibility error.
**Approach.** Host-fetch the RCSB biological assembly
(`https://files.rcsb.org/download/{PDB}.pdb1.gz`, or the assembly mmCIF) instead of
the AU `.cif`, OR `cmd.set('assembly','1')` before load in the PyMOL job; compute SASA
over the assembly; map candidate residues to the correct chain copy (author numbering
is preserved per copy). **Degrade-loud** when no biological assembly is defined: fall
back to the AU and NAME that accessibility is AU-based (never silently).
**Files.** `docker/pymol/_pymol_job.py`, `structural_reasoning_step.py:_fetch_structure`.
**Tests (real).** 2XFB: assembly-SASA differs from AU-SASA at known interface
residues; byte-stable across runs; no-assembly degrade note path.
**AC.** Candidate epitope residues classified exposed/buried in the **biological
assembly** context, with the assembly id recorded; AU fallback explicitly named.

## E3-2 — Taxon-precise structural Globus query [relevance correctness]
**Problem.** Free-text `q=<term>` lets a different virus's envelope rank high (real:
"chikungunya envelope" PDB top-10 includes **West Nile virus**); no taxon constraint.
**Real findings.** PDB records carry `pdb.polymer_entities[].scientific_name` (the
organism lever); **EMDB does NOT** (organism only in `titles`/`descriptions`); no
taxon id/IRI anywhere. Globus supports `@advanced` query_string field-scoping AND
structured `filters` (match_any) on nested fields — but the match is **EXACT,
case-sensitive, full-string** (organism has strain-qualified + case variants), so a
naive single-value filter under-recalls.
**Approach.**
- **PDB:** facet pre-pass on `pdb.polymer_entities.scientific_name` scoped by the
  species term → enumerate every spelling whose value contains the species name →
  `match_any` filter + `q=<protein/structural keywords>`. (Real: before 1162 w/ West
  Nile → after **9, all CHIKV**.)
- **EMDB:** `@advanced` `q` that REQUIRES the taxon token in
  `titles.title`/`descriptions.description` AND the structural keyword (hard AND, not
  soft OR), since no organism filter exists there.
- Consume `normalize`'s `taxon_id`/`protein`: map `taxon_id`→species name strings
  (reuse the `taxon_species` table / strain→species mapping), `protein`→`q` keywords.
- Degrade-loud when the taxon can't be resolved (fall back to free-text + a NAMED note
  that results are not taxon-locked).
**Files.** `structural_evidence_step.py:_search_source`,
`harmonized_search.py:_aggregate_served_search` (lockstep twins),
`agents/globus_search/_datacite.py` (+ `datacite_organisms` helper),
`agents/globus_search/client.py` (filters already advanced-capable).
**Tests (real).** before/after relevance (West Nile excluded); CHIKV-precise top-5;
EMDB required-token path; taxon-unresolvable degrade.
**AC.** A CHIKV envelope query returns only CHIKV-deposited structures; cross-virus
false-positives eliminated; the taxon constraint is a hard AND.

## E3-3 — Real functional validation (UniProt features + SIFTS + IEDB) [makes stage 3 real]
**Problem.** FunctionalValidationStep is an honest "named absence" — the bundle
carries no residue-level annotation.
**Real findings (all live-reachable 2026-06-13).**
- **UniProt REST features** (primary): residue-level features (Glycosylation =
  epitope-masking, Disulfide, Binding/Active site, Domain) — 33 features for 2XFB's
  `Q1H8W5`. No "ANTIGEN" feature type (use IEDB for that).
- **SIFTS** (`ebi.ac.uk/pdbe/api/mappings/uniprot/{pdb}`) — **MANDATORY bridge**: PDB
  author numbering → UniProt numbering (2XFB chain A resi 1 → UniProt **810**, per-chain
  offsets). PyMOL emits **author** numbering = SIFTS frame → they align. **RCSB
  `aligned_regions` uses label/entity numbering → WRONG → a silent off-by-hundreds
  trap. Use SIFTS for the offset, RCSB only to discover the accession.**
- **IEDB query-api** (bonus): known epitopes in **UniProt coords** (same frame as
  UniProt features — free once SIFTS bridges).
- **BV-BRC**: genome-level only — not useful here.
**Approach.** chosen PDB → UniProt accession → SIFTS per-chain residue bridge →
UniProt features + IEDB epitopes → cross-check each candidate epitope residue → emit
real coincidence ("residue N / UniProt M coincides with glycosylation" / "within IEDB
epitope X-Y") or honest "no feature at N". Feed via a new annotation helper into the
step's EXISTING `coincidences` scan seam (output contract unchanged). New small async
clients (`UniProtClient`/`SiftsClient`/`IedbClient`) modeled on `OLSClient` (httpx +
cache).
**Reliability.** Degrade-loud (no xref / network down → named note, never raise —
preserve G127). Cache PDB/SIFTS (immutable) indefinitely; UniProt by release; IEDB by
TTL + record query date in provenance. **Lock the SIFTS author-numbering bridge behind
a real-data fixture (2XFB chain A +809)** — the dominant silent-failure risk is a wrong
offset producing confident wrong coincidences.
**Tests (real).** 2XFB → real coincidence (glycosylation at a candidate residue OR
IEDB overlap); SIFTS offset fixture; degrade paths; IEDB PostgREST `cs.{}` query pinned.
**AC.** Candidate epitope residues cross-checked against REAL residue-level annotation
with verified numbering; coincidences reported or honest absence.

## E3-4 — Fully-automated Rhea bring-up in apecx-setup (+ push fork to main) [closes E2-R live gap]
**Problem.** The Rhea live path needs a running worker; the current `_step_rhea` is a
host-process path (needs a checkout + `uv` venv), opt-in. The directive: fully
automated, zero user vars, Docker.
**Real findings.** Worker = `python -m rhea.server.mcp_server --transport
streamable-http`, port 3001, `http://localhost:3001/mcp/`. The `synthesize_rhea_step`
(find_tools) path needs the **FULL stack**: Redis (at import) + Postgres pgvector
(find_tools RAG) + embedding (apecx uses **Ollama `mxbai-embed-large`**, not TEI — no
multi-GB pull) + an **ingested catalog**. The 3 sidecars are already started by
`setup.py:_step_infra`; `DockerMCPWorker` (nanobrain) is the right lifecycle manager
for the **worker container only** (reuse-probe + MCP-handshake health). `RHEA_MCP_URL`
already defaults to `http://localhost:3001/mcp/`.
**Critical risks (data-backed).** (1) **Container HOST-binding** — the server binds
`settings.host=localhost` → unreachable from host even with `-p`; MUST inject
`HOST=0.0.0.0` (the published image likely has this bug; the README marks
streamable-http "WIP"). (2) **Published `chrisagrams/rhea-server` ≠ fork** (lacks the
ToolShed config, `local` Parsl backend, MUSCLE fixes, rewritten `update_tools`) → MUST
**build from the fork**. (3) Parsl `local` backend needs conda in-image for tool
*execution* (discovery/find_tools does NOT). (4) Postgres host port is **5435** + use
`host.docker.internal`. (5) daemon-down → `StepResult(skipped)`, never raise. (6)
find_tools cold-catalog → **ingest required** (`update_tools.py`,
`RHEA_INGEST_ONLY=muscle` ≈ 10s) before the live path works. (7) first-run latency →
keep opt-in/background.
**Approach.**
- **Fork (push to main — authorized):** default `HOST=0.0.0.0` for streamable-http
  (fix the bind bug); ensure the Dockerfile bakes conda for local Parsl. Push.
- **apecx-setup:** rewrite `_step_rhea` to a Docker path: `docker build
  apecx-rhea-server` from the autodiscovered fork; reuse the 3 sidecar specs; run the
  worker with the orchestrator's env derivation
  (`DATABASE_URL→host.docker.internal:5435`, `REDIS_HOST`, `EMBEDDING_URL→Ollama`,
  `PARSL_CONTAINER_BACKEND=local`, **`HOST=0.0.0.0`**); health-check via
  `DockerMCPWorker.ensure_running()`; ingest via `docker exec … update_tools`
  (`RHEA_INGEST_ONLY=muscle` fast default); confirm `RHEA_MCP_URL`. Degrade gracefully
  when docker is down.
**Files.** `rhea/rhea/server/{schema.py,mcp_server.py}`, `rhea/Dockerfile`;
`apecx-mcp-integration/src/apecx_integration/cli/setup.py:_step_rhea`,
`infrastructure/{orchestrator.py,containers.py}`; nanobrain `DockerMCPWorker` (reuse).
**Tests (real).** post-setup gated integration: `synthesize_rhea_step(<real galaxy
tool>)` against the live worker resolves + asserts real determinism pins (closes the
E2-R live gap); setup idempotency.
**AC.** `apecx-setup` brings up a working Rhea worker with **zero user vars**;
`synthesize_rhea_step` resolves a real tool end-to-end; the E2-R live test passes.

## E3-5 — Verify the desktop/headless split with a REAL MCP client [honest verification]
**Honest status: PARTIAL.** The streaming BACKEND works + is real-data verified
(`run_workflow_streamed` callback + `run_workflow_streaming` FastMCP tool; stages
stream in order; streamed == headless; streaming failure can't break the run). **But
no real MCP client has consumed it** — only the test harness with a fake `Context`.
The "desktop application" is **undefined** (Claude Desktop as the MCP client? a bespoke
app? none exists). MCP-over-stdio end-to-end delivery to a real client is UNVERIFIED.
Using `send_log_message` to carry stage CONTENT (not just logs) is also a slight abuse
of the channel worth revisiting.
**Approach.** (a) Write a minimal REAL stdio MCP client that connects to `apecx-mcp`,
calls `run_workflow_streaming` with a `progressToken`, and renders the per-stage
progress+log — proving end-to-end delivery. (b) Document the "desktop" contract (which
client, what it renders, the notification schema). (c) Assess `send_log_message` vs a
cleaner MCP mechanism (a streamed resource / structured notification) and record the
tradeoff.
**Tests (real).** a real MCP client receives N stage notifications in order over
stdio; content matches the headless doc.
**AC.** A real MCP client demonstrably renders the live per-stage stream; the desktop
contract is documented; headless one-shot unchanged.

## E3 leftovers / cross-cutting (carried from v2 — the "anything else")
- **E3-6 (E2-A) single-source model resolver + preflight** — one `resolve_llm_model()`
  across factory/setup/composer; a LOUD preflight validating the model is pulled before
  first call (no cryptic Ollama 404). The 3-way divergence symptom is fixed; the root
  is not. [reliability]
- **E3-7 PyMOL image build in apecx-setup** — wire `docker build -t apecx-pymol:3.1.0
  docker/pymol/` into setup (mirror E3-4's Docker step) so structural reasoning has its
  real path out of the box. Closes the real-PyMOL-on-2XFB verification gap (once docker
  is up). [reliability/reproducibility]
- **E3-8 provenance capture** — record per run: chosen structure + ranking rationale,
  PyMOL version + SASA settings + assembly id, MAFFT version + conservation threshold,
  the structural query + taxon resolution, UniProt/SIFTS/IEDB query dates. For
  HPC-determinism reproducibility. [reliability]
- **E3-9 conserved-sites caching + perf** — content-address the conserved_sites result
  by (taxon, protein, aligner) so re-runs skip the ~6-min MAFFT; reduces the 480s
  inner-timeout fragility (margin, not guarantee). [perf/reliability]
- **E3-10 test-isolation fix** — 4 globus tests fail when run alongside the synthesizer
  suite (a leaked `SearchClient` mock/env), pass in isolation — masks real failures.
  Add fixture cleanup. [reliability]
- **E3-11 EnvelopeStepConfig conformance** — `extra='forbid'` + `COMPONENT_TYPE`
  (the one step config missing the workspace pydantic guard). [nanobrain conformance]
- **E3-12 Rhea G130 doc cross-ref** — register G130 in
  `apecx-mcp-integration/docs/nanobrain_capability_gaps.md` + the `nanobrain-agents-tools`
  / `nanobrain-lightweight` SKILLs (design already in nanobrain docs). [docs]
- **E3-13 (optional) multi-structure structural reasoning** — analyze the top-N ranked
  structures, not just the best, for epitope-surface robustness (different structures
  reveal different surfaces). [quality]
- **E3-14 (optional) model quality tier** — document/offer `mistral-nemo` as the
  quality tier; `nemotron-3-nano:4b` guarantees the contract but reasoning depth is
  shallow on a 4B model. [quality]
- **C4-5 split synthesis — DEFERRED** (the contract already does cross-data reasoning +
  integrated insight in one grounded pass); revisit only if the single-LLM-pass proves
  limiting in practice.

## Priority + dependencies
**Scientific-quality core (do first):** E3-2 (query precision — gates the relevance of
every structural result) → E3-1 (assembly accessibility — correctness of every SASA
call) → E3-3 (real functional validation). These compound: a precise taxon query
(E3-2) feeds the right structure, whose assembly SASA (E3-1) yields candidate residues,
which E3-3 cross-checks against real annotation.
**Automation/reproducibility:** E3-4 (Rhea, closes the live gap) + E3-7 (PyMOL image)
make the two externally-blocked paths runnable out of the box; E3-8 (provenance) makes
runs reproducible.
**Surface verification:** E3-5 (real MCP client).
**Reliability/conformance hygiene:** E3-6, E3-9, E3-10, E3-11, E3-12.
All new components ship from_config + process()-only, degrade-loud (G127), with a
real-data integration test (no mock-only "done"); every external-API client caches +
degrades loud. Framework gaps (if any) ship as nanobrain capability + skill +
regression test.
