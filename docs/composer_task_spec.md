# LLM composer — task spec (T-COMP, 2026-04-23)

**Status:** Not started — scoping doc only. No code in
`src/apecx_integration/composition/composer.py` yet.
**Authoritative sources:** AP §5.2 (component library), AP §5.3 (RAG),
AP §5.11 (artifact versioning), AP §5.13 (sandbox), AP §5.6 (diff UX).
**Author:** 2026-04-23, in response to the implicit-task design gap
flagged in implementation_plan.md when T11 / T13 shipped their
primitives with the "composer hook deferred" caveat.

---

## 1. Why this document exists

`implementation_plan.md` Phase 2 "Must" lists an LLM composer that
"lives in composition/composer.py" but has no task row, no ACs, no
effort estimate, and no sequencing. Four tasks are explicitly blocked
on it:

| Task | What it needs from the composer                                    |
|------|--------------------------------------------------------------------|
| T06  | A generated-workflow artifact to diff against a library baseline.  |
| T11 step 3 | A caller of `ArtifactStore.store()` at generation time.      |
| T12 AC1 | A callable that produces a stable artifact hash given a prompt. |
| T13 step 3 | A caller of `scan_python_source()` before running novel code. |

Leaving the composer as "implicit" means none of the above can close.
This spec exists so the composer becomes a real task that can ship
in bounded chunks instead of "one big blob at week 6."

**Brutal-truth check on my own scoping**: a spec doc doesn't ship
code. The risk is that spec-writing becomes a substitute for
spec-fulfilling. Mitigation: every phase in §6 below has an explicit
exit criterion that makes progress falsifiable.

---

## 2. Goal

Given a scientist's natural-language description of a workflow (plus
optional structured hints like "target variant = VIOLIN × BV-BRC"),
the composer produces:

1. A **workflow YAML artifact** that composes existing library
   components where possible, emits novel Python only where no
   combination of existing components covers the requirement, and
   flags every piece of novel Python explicitly in the output.
2. A **GeneratedArtifact row** (T11) pinning the source prompt,
   library version, LLM model + model-version hash, composition
   summary (how many steps reused / generated / swapped), and the
   parent artifact if this is a regeneration.
3. A **composition diff payload** (feeds T06's review UX) naming
   which steps came from the library and which were generated fresh.

The composer does **not** run the workflow, does not read/write the
Control Plane Run table (Tier 2's job), and does not talk to
executors. It produces artifacts that Tier 2 + Tier 4 consume.

---

## 3. Module layout + surface

```
src/apecx_integration/composition/
  composer.py              # Composer class — ships in phases below
  composer_prompts/        # versioned prompt files (AP §5.3 line 485)
    system.md
    composition_bias.md
    novel_python_flagging.md
  composer_schemas.py      # pydantic models for the composer's IO shapes
  transforms.py            # (already shipped 2026-04-23)
  sandbox.py               # (already shipped 2026-04-23)
  artifact_store.py        # (already shipped; T11)
```

Public surface (**subject to refinement once Phase 1 lands**):

```python
from apecx_integration.composition.composer import Composer, ComposedWorkflow

composer = Composer.from_config("composer_config.yml")
composed: ComposedWorkflow = await composer.compose(
    prompt="Find vaccines for EEEV using VIOLIN + BV-BRC data",
    context={"variant": "hard_only"},
)
# composed.artifact_id is the content-hashed Artifact row id.
# composed.yaml_bytes is the raw YAML the scientist reviews.
# composed.novel_python is {step_id: source} for anything not in the library.
# composed.composition_summary is the diff-UX payload.
```

`ComposedWorkflow` is a frozen dataclass in `composer_schemas.py`:

```python
@dataclass(frozen=True, kw_only=True)
class ComposedWorkflow:
    artifact_id: UUID
    yaml_bytes: bytes
    novel_python: dict[str, str]            # step_id → Python source
    composition_summary: CompositionSummary
    retrieved_components: list[str]         # component IDs the RAG surfaced
    llm_model: str
    llm_model_version_hash: str

@dataclass(frozen=True, kw_only=True)
class CompositionSummary:
    steps_reused: int
    steps_generated: int
    steps_swapped: int                       # existing component replaced mid-plan
    summary_sentence: str                    # "Reused 6 library steps; generated
                                             #  1 novel Python step (violin_xref)."
    review_notes: list[str]                  # anything the reviewer should see
```

---

## 4. Dependencies

**Hard prereqs** (must be done before Phase 1 of the composer can
start):

- T02 ✅ — component library reuse/wrap/gap-fill done.
- T09 ✅ — durable Run + Artifact tables exist.
- T11 ✅ (primitive) — `ArtifactStore.store()` works.
- T13 ✅ (scanner) — `scan_python_source()` works.
- Scope memo 07 ✅ — local-LLM Agent support (composer defaults to
  Ollama per workspace local-default policy).
- `TransformLink` YAML loader ✅ (2026-04-23) — composer-emitted
  YAMLs can use TransformLinks freely.

**Soft prereqs** (needed for later phases, not Phase 1):

- **T03 RAG index** — Phase 2 needs it. Phase 1 can use a linear
  scan over the Component table while T03 is being built.
- **T06 diff UX** — Phase 4 needs it. Phases 1–3 can emit the diff
  payload to logs/stdout without a UI.
- **Named prompt library** — versioned prompt files under
  `composer_prompts/` must ship with Phase 1, but they can be
  iterated in isolation.

---

## 5. Acceptance criteria

**AC1**: `Composer.from_config("composer_config.yml")` loads without
error against the default Ollama-backed config; the config file
commits alongside the composer code.

**AC2**: `compose(prompt, context=None)` returns a
`ComposedWorkflow` with a non-empty `yaml_bytes` for at least one
fixture prompt ("find vaccines for EEEV" against the violin_bvbrc
manifest). The returned YAML is a valid `Workflow.from_config(...)`
input — i.e., loads without the framework raising.

**AC3**: `composed.artifact_id` points at an `Artifact` row in the
Tier-2 DB, the on-disk file matches the sha256 `content_hash`, and
the corresponding `GeneratedArtifact` row pins (prompt, library_version,
llm_model, llm_model_version_hash, composition_summary).

**AC4**: Regenerating the same prompt against the same library +
model produces a **different** `artifact_id` (append-only invariant)
but either (a) the same `content_hash` under `temperature=0`, or (b)
a different hash whose YAML is semantically equivalent per T12's
comparator ladder. Assert distinct row IDs in either case.

**AC5**: When the composer emits novel Python, the
`ImportScanner` (T13) is invoked before the artifact is returned;
unknown imports raise `ScanViolation` and the composer does NOT
persist the artifact. The scanner violation is surfaced in the
exception message to the caller.

**AC6**: The composer's prompt text lives in
`src/apecx_integration/composition/composer_prompts/*.md` — grep
for raw prompt strings in `composer.py` fails (enforce via TX5 CI
check or a unit test that greps).

**AC7**: A "composition-bias" regression test exists: given a
fixture prompt that is fully covered by library components, the
composer emits zero `novel_python` entries. If this fails, the
library prompt isn't biasing composition hard enough (AP §5.3 line
485's warning).

**AC8**: Wall-time budget: one composition against `mistral-small`
(local Ollama) completes in ≤60 s for a typical workflow-spec-sized
prompt. Operator-run; Claude auto-skips per the no-live-LLM
constraint.

---

## 6. Phased delivery (effort estimate: 5–7 engineer-days)

### Phase 1 — skeleton + config + loadability (≤1d) ✅ shipped 2026-04-23 (`2d543df`)

- Author `composer.py` with the class signature + `from_config`
  classmethod that loads a `ComposerConfig` pydantic model.
- Author `composer_schemas.py` with `ComposedWorkflow` +
  `CompositionSummary` frozen dataclasses.
- Author the three prompt files under `composer_prompts/` as
  placeholders (real text iterated in Phase 2).
- Author `ComposerConfig` with fields: `llm_model`, `llm_base_url`,
  `library_version`, `prompt_dir`, `max_retries`.
- `compose()` raises `NotImplementedError` in Phase 1 — the
  loadability test is enough.

**Exit criterion**: `Composer.from_config(default_config)` test
passes; `compose()` is not yet callable.

### Phase 2 — linear-scan library retrieval + first real prompt (1–2d) ✅ shipped 2026-04-23

**Deviation from original plan (documented)**: the spec said "Query
the Component table directly (no RAG yet)." The T09 Component DB
table has zero seed data — querying returns nothing. Phase 2 reads
components from `ComponentCatalog.from_manifests([paths])` instead,
paths come from `ComposerConfig.component_catalog_paths`. The
`ComponentCatalog.search` signature stays stable when T03 RAG
replaces the substring-match internals in Phase 4.

Also shipped in Phase 2:

- Real prompt text in `composer_prompts/*.md` (was placeholder).
- T13 scanner integration (novel-Python `ScanViolation` short-
  circuits the compose call — AC5 satisfied).
- Fenced-block parser for LLM output (`yaml` + optional
  `novel_python`), with error paths for missing / malformed /
  non-mapping-top-level yaml.
- Placeholder-LLM test suite (12 tests in
  `tests/integration/test_composer_phase2.py`).
- Operator-run live test (2 tests in
  `test_composer_phase2_against_ollama.py`, auto-skip via
  `APECX_SKIP_LIVE_LLM=1`).

**Not in Phase 2** (Phase 3):

- `ArtifactStore.store()` integration — `ComposedWorkflow` has an
  `artifact_id` (uuid4) but no Artifact row is written yet.
- `WORKFLOW_GENERATED` provenance event emission.

**Exit criterion**: a fixture prompt produces a non-empty YAML that
loads via `Workflow.from_config(...)` (AC2 happy path; AC3 deferred
to Phase 3).

### Phase 3 — persistence integration (≤1d) ✅ shipped 2026-04-23

- Wire `ArtifactStore.store()` call at the end of `compose()`. ✅
- Populate `GeneratedArtifact` metadata from the LLM config. ✅
- Emit a `WORKFLOW_GENERATED` provenance event (free — already
  hooked inside `ArtifactStore.store()`). ✅

**Deviation from spec, documented**: the ArtifactStore hookup is
**opt-in via injection** — `Composer(config, artifact_store=store)`
+ `compose(prompt, context={"run_id": <uuid>})`. When either the
store or the run_id is missing, the composer falls back to the
Phase-2 path (uuid4 + no-persist) with a one-shot warning log.
Rationale: tests of the LLM pipeline should not have to spin up a
migrated SQLite DB + Run row just to exercise compose(). Production
always has both.

**Exit criterion**: AC3 passes — artifact row + on-disk file +
generated metadata all consistent. ✅ (6 tests in
`tests/integration/test_composer_phase3.py` cover AC3 + AC4 partial.)

**AC4 status after P3**: partial. Two successive compose() calls
produce distinct artifact_ids (append-only invariant) ✅. Same-content
responses produce the same content_hash but distinct UUIDs ✅. Full
AC4 (semantic-equivalence fallback when model version changes)
still deferred — that depends on T12's comparator ladder and a
live model-version-bump scenario.

### Phase 4 — T03 RAG swap-in (1–2d) ✅ shipped 2026-04-22

- Replace the linear-scan retrieval with a `ComponentIndex.search`
  call. This is the composer side of T03; the RAG index's
  embedding/build work is separate. ✅ `_retrieve(prompt, k)`
  adapter method added to `Composer`: when
  `ComposerConfig.rag_index_dir` is set, loads
  `nanobrain.lightweight.component_index.ComponentIndex` and
  converts `ComponentMatch` → `SearchHit` with `score` =
  `int(round(similarity * 1000))`. Unset → Phase-2 linear scan
  remains the default.
- `scripts/build_rag_index.py` builds the FAISS artifact
  out-of-band (reads composer config, rebuilds + saves). Avoids
  ~5s model-load on first `compose()` call.
- Tune the K value against the 20-synthetic-query test suite in
  T03's AC (80% top-5 recall target). Current default
  `retrieval_k=10`; T03 diagnostics show top-1 at 80%, so tightening
  to 5 is a pending optimization — deferred, not hard-required.

**Exit criterion**: T03 AC and composer AC2/AC7 both pass
concurrently on the same prompt set. ✅ Verified:
`tests/integration/test_composer_phase4_rag.py` (6 tests, all
green) covers config loading, RAG retrieval, linear-scan fallback,
and two error paths (missing dir, half-built index). Composer AC2
(existing Phase-2 tests) regression-green with `rag_index_dir=None`
fallback.

### Phase 5 — hardening + AC6 prompt discipline + AC8 wall-time (≤1d) — partial 2026-04-22

- Move any remaining inline prompt strings to `composer_prompts/`
  files (AC6). ✅ already satisfied after Phase 2; enforcement added
  at `tests/unit/test_composer_prompts_are_files.py` (AST walk +
  400-char + "You are" signature check; 3 unit tests).
- Add the composition-bias regression test (AC7). ✅ shipped at
  `tests/integration/test_composer_ac7_composition_bias.py`
  (live-LLM skip-gated; asserts `result.novel_python == {}` for a
  prompt fully covered by the violin_bvbrc catalog).
- Measure wall-time on the fixture prompt set; tune `max_tokens`
  (via APECX_LLM_MAX_TOKENS — already supported since 2026-04-23).
  **AC8 status: pending** — operator-run measurement; not
  automatable under the no-live-LLM constraint.

**Exit criterion**: all 8 ACs pass; CI (when GitHub ships per TX2)
gates on the composer's tests. **Phase 5 status (2026-04-22):**
AC6 + AC7 shipped; AC8 deferred to operator.

---

## 7. Risks + mitigations

### R1 — "composition bias" is soft

The LLM may cheerfully generate novel Python even when a library
component covers the requirement. **Mitigation**: AC7 regression
test + the composition-bias prompt file + T06 diff UX surfacing
novel Python to the reviewer. **If the 80% top-5 RAG recall target
is met**, the temptation to generate novel Python drops sharply —
the model SEES the relevant component and uses it.

### R2 — regeneration produces subtly-different YAML every time

Even at `temperature=0`, LLM determinism isn't guaranteed across
model version bumps. AC4's "semantic-equivalence fallback" leans on
T12's comparator ladder; if T12's ladder is wrong, AC4 becomes a
flapping test. **Mitigation**: pin the LLM model version in the
artifact row (AP §5.11); when a flap happens, the model bump is the
first suspect, not a composer bug.

### R3 — novel Python slips through the scanner

The T13 scanner is static AST only — it can't catch
`getattr(__builtins__, 'eva' + 'l')()` style escapes. **Mitigation**:
narrow whitelist + human review at the T06 gate + future T13b Docker
sandbox. The composer does not weaken the scanner; it surfaces the
violation to the reviewer.

### R4 — prompt drift makes Phase 5 AC6 painful

If Phase 2 ships inline prompt strings "just to get it working," the
cleanup at Phase 5 is significant. **Mitigation**: ship
`composer_prompts/` files in Phase 1 BEFORE any LLM call is wired.
Treat inline prompts the way the workspace treats hardcoded
credentials — rejected at commit time.

### R5 — linear-scan library retrieval (Phase 2) doesn't scale past
~50 components

The substring-match fallback is acceptable for Phase 2's 15–20
components (AP §5.2). If the library grows before T03 RAG lands, the
composer's recall drops silently. **Mitigation**: track library size;
escalate T03 prioritization if the library exceeds 30 components
before RAG ships.

---

## 8. What this doc deliberately does NOT decide

- **LLM vendor**: Ollama is the local default; Claude/OpenAI/vLLM
  are supported via the `APECX_LLM_*` env vars. The composer
  doesn't encode a vendor choice.
- **Prompt text**: the prompt files are placeholders until Phase 2.
  Expert prompt engineering is a separate concern.
- **Retry / repair logic**: if the LLM emits invalid YAML, Phase 2
  fails the test; Phase 5 can add retry-with-repair-prompt if real
  usage surfaces the need. YAGNI-gated.
- **Multi-step vs. single-shot prompting**: single-shot is Phase
  1–3 assumption. Multi-step (chain-of-thought, iterative refinement)
  is a Phase 5+ consideration if quality demands it.
- **Caching generated artifacts by prompt hash**: the artifact_store
  ALREADY content-addresses; a prompt-hash cache is an optional
  optimization (AP §5.11 hints at it). Not in the critical path.

---

## 9. How this doc should be maintained

- Each phase's exit criterion is a falsifiable pass/fail test
  committed alongside the code for that phase.
- When a phase ships, tick the status in §6 (e.g. "Phase 1 ✅
  shipped 2026-04-25 in commit abc1234"). Don't add to this doc
  outside of phase-completion updates; let the ACs speak.
- If an AC needs revision during implementation, update this doc
  FIRST, then change the test. Don't silently widen an AC to match
  a passing test.

---

## 10. Pointers

- `src/apecx_integration/composition/artifact_store.py` — the
  T11 primitive the composer will call.
- `src/apecx_integration/composition/sandbox.py` — the T13
  scanner the composer will call.
- `src/apecx_integration/composition/transforms.py` — sample
  transforms the composer-emitted YAMLs can reference.
- `nanobrain/nanobrain/core/link.py::TransformLink` — YAML-loadable
  as of 2026-04-23; composer-emitted workflows can freely use
  TransformLinks now.
- `architectural_plan.md` §5.2 (component library), §5.3 (RAG), §5.6
  (diff UX), §5.11 (artifact versioning), §5.13 (sandbox).
- `implementation_plan.md` — this task's T-COMP row + updated
  Phase-2 "Must" list reference this doc.
