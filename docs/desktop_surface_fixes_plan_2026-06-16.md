# Desktop-surface fixes — investigation + fix plan (2026-06-16)

Branch: `epitope-desktop-surface-fixes` (worktree `wt-surface-fixes`, off `epitope-retrieval-distillation`).

Four reported issues, each investigated by a dedicated agent against the real code (file:line
grounded; live Globus where noted). Status tags: **VERIFIED** (confirmed by reading code / running),
**HYPOTHESIS** (strong inference, not yet reproduced), **NEEDS-VERIFY** (must reproduce before fixing).

Two findings overturned the original framing:
- **Issue 1 is a visibility problem, not a data-drop** — per-source counts already reach the doc, buried.
- **Issue 4 is NOT in `viral_epitope_analysis`** (already free-text-correct) — it's the sibling
  `viral_conserved_sites` / `_muscle` tools that still demand a `taxon_id`.

---

## Issue 1 — Globus per-source count missing from the final document

**Symptom:** the final report doesn't show per-source (per-Globus-index) record counts.

**Root cause (VERIFIED):** NOT a data-flow drop. `harmonized_search_summary.per_index_kept`
(set in `harmonized_bundle_merge_step.py:181-185`, full counts after f4b07a5) DOES reach the final
document — but only as a single collapsed bullet inside `### Reasoning trace` (the `data_readiness`
stage report, rendered by `_stage_report.render_stage_reports` → `evidence_review_synthesis_step.py
:341-351,487-489`). The two *prominent* Globus surfaces both hide the breakdown:
- the distillation note (`evidence_distillation_step.py:296-318`) ranks all 9 indices as ONE pool
  "globus" → `globus 20/13397` (kept/retrieved digest, not per-source coverage);
- the deterministic `## Sources and evidence` section lists records but carries no per-source totals.

So a reader sees one aggregate Globus number and concludes the per-source counts are absent.

**Terminal synthesis path (VERIFIED):** `viral_epitope_analysis` ends `review`
(`EvidenceReviewSynthesisStep`) → `gate` → `envelope`; the doc is built by
`compose_evidence_markdown(...)` (`evidence_review_synthesis_step.py:472-496`). It does NOT use
`RagSynthesisStep` (that's `rag_e2e_synthesis` only). Desktop vs headless produce the SAME doc
(the "defer to host" scaffold was removed in 72e013b).

**Fix:** add a pure `render_coverage_section(bundle)` reading `bundle["data_readiness"]["counts"]`
(or `harmonized_search_summary.per_index_kept`), emitting a prominent `## Evidence coverage`
per-source list, inserted in `compose_evidence_markdown` (just above `## Sources and evidence`).
Render-only; data already on the bundle; degrade-safe guard for missing/empty counts.

**Files:** `composition/steps/evidence_review_synthesis_step.py` (new helper + insert).
**Safe:** `EvidenceReviewSynthesisStep` is epitope-only; `SynthesisContextAssemblyStep` /
`RagSynthesisStep` (shared with rag_e2e) untouched.
**Verify:** unit test asserting the coverage section renders the per-index counts; real e2e doc shows
`## Evidence coverage` with bvbrc_genome/protein/violin_* numbers.

---

## Issue 2 — Globus references to specific objects stripped (HIGHEST SEVERITY)

**Symptom:** the report loses concrete object IDs (PDB, GenBank/RefSeq, UniProt, BV-BRC IDs, DOIs),
so a claim can't be traced to a specific database object.

**Root cause (VERIFIED via live records) — TWO independent losses:**

1. **Projection strips every ID** (`harmonized_search_execute_step.py:320-342`). `_summarize_record`
   keeps only `title` + `subjects` (taxon-name strings). Its `identifier` branch (339-341) is DEAD
   CODE against the real DataCite shape — records have no top-level `identifier` key, so it never
   fires. Real records carry the IDs elsewhere, all dropped:
   - genome: `alternateIdentifiers` = `[{GenBank: KY703959}, {BVBRC-Genome: 37124.51}, {NCBI-Taxonomy: 37124}]`
   - structure: `alternateIdentifiers` = `[{PDB: 2XFB}, {PDB: 2XFC}, {UniProt: Q1H8W5}, ...]`
   - DOIs would live in `relatedIdentifiers` (type DOI); taxon IRIs in `subjects[].valueUri`.
   Projected genome record reduces to `{"title": "...", "subjects": ["Chikungunya virus", "3426298"]}`.
   (Also: `_fetch_records:167` keeps only `entries[0].content`, discarding the gmeta-level `subject`.)

2. **Render shape mismatch** (`synthesizer.py:535-574`, `evidence_review_synthesis_step.py:262-279`).
   Both renderers key off `h["subject"]` + `h["content"]` (the `globus_search/client.py:189-195`
   shape), NOT the flat `_summarize_record` shape. A projected harmonized record has neither →
   it hits the `if not subject: continue` guard and is **SKIPPED ENTIRELY** (vanishes, not even
   "(untitled)"). ⇒ **NEEDS-VERIFY (P0):** confirm whether the harmonized-path `globus_results`
   currently render at ALL in `## Sources and evidence`. If the agent is right, the 13k-record
   corpus wired in f4b07a5 may not be surfacing any records — a severe regression hiding behind a
   non-empty distill note.

**Fix:**
- Add DataCite reference extractors in `agents/globus_search/_datacite.py`:
  `datacite_identifiers(content) -> {type: [ids]}` (alternateIdentifiers + DOI relatedIdentifiers),
  `datacite_primary_id(content) -> str|None` (precedence PDB→GenBank→BVBRC→DOI→taxon-tail; a CLEAN,
  whitespace-free citation token), `datacite_taxon_iris(content)`.
- In `_summarize_record`, replace the dead `identifier` branch with `subject` = `datacite_primary_id`
  (also fixes the render-skip) + `identifiers` = typed dict. Keep the payload lean (typed dict, not
  the nested blocks — respect the distillation step's purpose).
- Surface IDs at render in BOTH renderers, handling BOTH record shapes (flat projected via
  `h.get("identifiers")`; `{subject,content}` via `datacite_identifiers(h["content"])`), e.g.
  `PDB:2XFB · GenBank:KY703959`.

**Risks/notes:** two record shapes coexist in `globus_results` (flat vs `{subject,content}`) — renderers
must handle both. UniProt values doubled (`Q1H8W5;Q1H8W5`) — split/dedupe on `;`. Any synthesized
`subject` must stay clean (the citation regex rejects `[`, `]`, whitespace). Re-check
`_score_globus`'s `pdb:`/`emdb:` prefix bonus against the new token (`evidence_distillation_step.py:164-166`).

**Files:** `harmonized_search_execute_step.py:167,320-342`; `_datacite.py`; `synthesizer.py:535-574`;
`evidence_review_synthesis_step.py:160-189,262-279`.

---

## Issue 3 — Streaming gives no per-step updates; tool times out

**Symptom:** desktop `run_workflow` streams nothing per step; the client times out (~90-150s run).

**Root cause (VERIFIED) — notifications gated on a payload only the back-half produces:**
`_make_stage_streamer` (`eo_primitives.py:373-415`) forwards a notification ONLY for a `step_complete`
whose `outputs` carries a `stage_reports` list (377-381). Only back-half top-level steps call
`append_stage_report` (synthesis_context_assembly, structural_evidence, sequence_merge,
structural_reasoning, functional_validation, distillation, rhea_genomic, data_readiness). The slow
FRONT — `normalize → resolve → map` (9 concurrent Globus index searches) `→ assemble` (unbounded
PubMed) — and ALL nested subworkflow time emit NOTHING. First possible notification ≈ after `assemble`.

- **Nested events propagate but are discarded (VERIFIED):** `subscribe_to_step_events` uses a
  ContextVar (`nanobrain/core/step_events.py:98-100`); inner steps of `map`/`sequence` run in the
  same loop/task (`subworkflow_step.py:553`, `map_subworkflow_step.py:183`) so their `step_complete`
  reaches the top subscriber — but they carry no `stage_reports`, so the filter drops them.
- **`report_progress` no-ops without a client progressToken (VERIFIED in contract doc):** if the
  desktop client didn't pass a progress callback, only `send_log_message` fires — a `logging/*`
  channel a client may route to a debug console, so the user perceives "no updates."
- **Timeout (HYPOTHESIS):** no server/FastMCP timeout; nanobrain `Workflow.run` default is 600s
  (`workflow_registry.py:153`) >> the run. So the timeout is CLIENT-side (~60s default). MCP clients
  reset that timer on progress notifications; with no progress for the first 60s+ (silent front +
  possibly no-op `report_progress`), the client times out before the first stage. Lack of streamed
  progress IS what trips the timeout.
- Loop-blocking RULED OUT: heavy steps use `asyncio.to_thread` / subprocess; the loop yields.

**Fix (layered):**
1. Per-step heartbeats decoupled from `stage_reports`: broaden the subscriber so every
   `step_start`/`step_complete` (top-level AND inner) emits a lightweight `report_progress`
   heartbeat naming the step; keep the rich `send_log_message` stage card only when `stage_reports`
   present. Throttle/coalesce inner heartbeats (map fans 9×) to avoid flooding; keep all emits
   wrapped so observability can't break the run. (`eo_primitives.py:373-415,485-506`)
2. Early + periodic progress: emit `report_progress(0, "starting <name>")` on tool entry (~508) and
   a keepalive if no event arrived within N seconds. (`eo_primitives.py:447-516`)
3. (Additive) emit stage reports from the front/nested phases (e.g. a post-map "harmonized search:
   N hits across 9 indices", MAFFT "N regions") so the content channel is non-silent during the
   front. apecx step wrappers only (nanobrain library classes are closed-class).
4. Doc drift: `docs/desktop_streaming_contract.md:8-9,44-48` still names a separate
   `run_workflow_streaming` tool; the real tool is `run_workflow` (server.py:169).

**NEEDS-VERIFY:** the exact client-side timeout + whether Claude Desktop resets on `report_progress`
vs requires a progressToken — confirm against a real desktop session or the FastMCP transport before
committing to fix #2's mechanism. Reproduce "no heartbeat in first 60s" with a local streamed run.

**Files:** `eo_primitives.py` (streaming section); `server.py`; `docs/desktop_streaming_contract.md`;
verify nested propagation already works (no nanobrain change expected).

---

## Issue 4 — Workflow requires taxon_id instead of free-text; harmonized search run separately

**Symptom:** on desktop, the user must run harmonized search first to get a taxon_id, then feed it
to the workflow, instead of passing a free-text query.

**Root cause (VERIFIED) — wrong workflow blamed; it's the SIBLING conserved-sites tools:**
`viral_epitope_analysis` is ALREADY correct (115fbf3 fully landed): catalog
`mcp_workflow_catalog.yml:224-250` `required:[query]`, `taxon_id` optional; builder
`EVIDENCE_INPUT_SCHEMA` (builder.py:116-163) says "do NOT pre-resolve a taxon_id"; `how_to_run`
(eo_primitives.py:673-712) says pass a bare name. Empirically `find_param_gaps({"query":...})==[]`.

The gap is `viral_conserved_sites` and `viral_conserved_sites_muscle`
(`mcp_workflow_catalog.yml:98-145,146-194`), BOTH first-class desktop tools AND in `list_workflows`:
1. `input_schema.required: [taxon_id, protein]` — **no `query` param at all**.
2. Descriptions verbatim: *"Resolve the virus name to an NCBI taxon_id FIRST (e.g. via
   harmonized_search), then call with {taxon_id, protein}."*
3. Their builder is `fetch → align → conserve → report → envelope` — no resolve step, `fetch_in`
   takes `taxon_id` directly.
A model answering "conserved sites/epitopes on CHIKV E1" plausibly routes to the name-matching
`viral_conserved_sites`, whose contract forces a separate `harmonized_search` → taxon_id first.
Reinforced by `harmonized_search` being advertised as "canonical one-shot retrieval"
(`_PRIMITIVES`, eo_primitives.py:57-62).

**Fix (recommended — option 1):** mirror the epitope resolve fix onto the conserved-sites builder
(`composition/workflows/viral_conserved_sites/builder.py`): prepend a resolve stage (reuse
`EpitopeResolveStep` / `harmonized_index_search` pattern) so the entry DU accepts `{query, protein,
taxon_id?}` and derives taxon_id; keep `taxon_id` as an optional override. Update the two catalog
entries (`query` required, taxon_id optional, new `input_envelope_key`) and rewrite descriptions to
drop the "resolve via harmonized_search FIRST" steer. Tone down `harmonized_search`'s "canonical
one-shot retrieval" framing so the model treats it as a lookup, not a mandatory pre-step.

**Alternative (option 3, lower-effort):** retire `viral_conserved_sites`/`_muscle` from the desktop
catalog (they overlap with `viral_epitope_analysis`, which already nests the conserved-sites
pipeline) OR rewrite descriptions to point free-text callers at `viral_epitope_analysis`. Surgical,
but removes a dependency-light (mafft-only, no Docker) path.

**Risk:** option 1 touches a builder shared by two catalog tools — re-verify both load + run e2e
(real BV-BRC + MAFFT/Rhea) and the new resolve stage degrades loud on an unresolvable name.
**Files:** `composition/workflows/viral_conserved_sites/builder.py`; `mcp_workflow_catalog.yml:98-194`;
`eo_primitives.py:57-62`.

---

## Sequencing, dependencies, coordination

- **Issues 1 + 2 share the terminal renderer** (`evidence_review_synthesis_step.py` Sources/coverage
  sections) — do TOGETHER to avoid two passes over the same render code. Issue 2 also re-touches
  `harmonized_search_execute_step.py:_summarize_record` (the file changed in f4b07a5) — coherent.
- **Issue 2 is highest severity** (P0 NEEDS-VERIFY: are harmonized records rendering at all?) — verify
  first; it may also explain part of Issue 1's "missing" perception.
- **Issue 3 is independent** (streaming, `eo_primitives.py`) — parallelizable.
- **Issue 4 is independent** (conserved-sites builder + catalog) — parallelizable; decide option 1
  vs 3 first (a product call: keep conserved-sites as a distinct lean tool, or fold into epitope?).

## Verification strategy (per workspace rules — real data, recorded commands)

- Unit: per-issue (coverage section render; `_summarize_record` ID preservation; `datacite_*`
  extractors on real record fixtures; streamer heartbeat emission; conserved-sites free-text schema).
- Integration (real): full `viral_epitope_analysis` e2e — assert `## Evidence coverage` present with
  per-index counts (1), Sources section cites PDB/GenBank/DOI tokens (2). Conserved-sites e2e with a
  free-text query → resolves internally, status=ok (4). Streaming: a local streamed run shows
  heartbeats from t≈0 (3) — full client-timeout confirmation needs a real desktop session.

## Open product decisions (need user input)

1. Issue 4: option 1 (add resolve to conserved-sites) vs option 3 (retire/redirect them)?
2. Issue 1: a dedicated `## Evidence coverage` section, or fold per-source counts into the existing
   `## Sources and evidence` header?
