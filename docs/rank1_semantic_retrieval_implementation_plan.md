# Rank 1 — detailed implementation plan (Phase 1b): semantic retrieval + name-based reasoning-pattern binding

> **STATUS: FUTURE WORK — detailed plan, not yet executed.** This is the Phase-1b expansion (per the `/feature` skill) of Rank 1 from `workflow_crafting_intelligence_roadmap.md`. No code is changed by this document. Execution must run the full `/feature` flow (Phase 1 EnterPlanMode → 1b → 2 implement → 3 self-review → 4 review-gate agent → 5 commit with `Reviewed:` trailer). The review gates are specified per-workstream below — they are part of the plan, not an afterthought.

**Repos:** `wt-epitope-artifacts` (apecx-mcp-integration) + `nanobrain` (WS1c touches `subworkflow_step.py`). Cross-repo ⇒ separate commits, nanobrain lands first.
**Grounding:** every file:line + snippet below is verbatim-verified this session.

---

## 0. BRUTAL TRUTH — three roadmap assumptions the verbatim code overturned

My own roadmap rated R1 as "build an embeddings index (M effort)." Reading the code, that's **wrong in three ways** — two tasks are nearly free, one hidden blocker is the real work:

1. **Semantic retrieval is ALREADY BUILT and wired — it's just switched off.** `Composer._retrieve()` (`composer.py:406-433`) already dispatches to a FAISS `ComponentIndex` when `config.rag_index_dir` is set, else linear scan. The index load (`composer.py:208-235`), the FAISS backend (`nanobrain/.../component_index.py:148-277`), and the builder (`scripts/build_rag_index.py:64-127`) all exist and are production-ready. **The only thing missing is the `rag_index_dir` key in `composer_config.yml` + the built artifact.** WS1a is therefore a config + artifact + staleness-guard task, *not* "build an index." I overestimated it.

2. **The real blocker for "reuse over a broad corpus" is not the scanner — it's missing manifests.** The composer corpus = `component_catalog_paths` = **exactly 2 manifests** (`composer_config.yml:39-41`). `discover_workflows()` (`workflow_discovery.py:190-209`) already auto-scans all ~30 workflow dirs — but for the *runnable* list, and most workflows **have no `manifest.yml` with `rag_description`**. So "auto-discover the corpus" finds almost nothing to add unless we *synthesize* catalog entries from discovered workflows. That synthesis (WS1b) is the actual work, and it's a design decision the roadmap glossed.

3. **`SubworkflowStep` already has TWO binding seams; the name seam is a third — and it can't import apecx.** `SubworkflowStepConfig` already has `inner_workflow_path` (`subworkflow_step.py:85-95`) AND `inner_workflow_builder` (`:97-112`) with mutual-exclusion validation (`:254-269`). The new `inner_workflow_name` seam is well-scoped — BUT `subworkflow_step.py` lives in **nanobrain**, which must not import apecx's `discover_workflows`. So the resolver must be nanobrain-native (a `workflow_search_paths` config apecx populates), not a reach into apecx. The roadmap's "add a name-based seam (M)" hid this layering constraint.

**Net honest re-estimate:** WS1a ≈ S (turn on + staleness guard), WS1b ≈ M (manifest synthesis — the real work), WS1c ≈ M (nanobrain seam + resolver). Smaller than the roadmap implied on retrieval, but with a hidden corpus-coverage problem.

---

## 1. Scope & "what I will NOT build" (Phase 1)

**In scope (Rank 1 only):** turn on semantic retrieval (WS1a); make the composer corpus auto-cover all discovered workflows as reusable sub-steps (WS1b); add `inner_workflow_name` binding so a reasoning-pattern workflow can be referenced/composed by name (WS1c).

**What I will NOT build (explicit):**
- No AFLOW/MCTS meta-search (roadmap R6+, deferred — needs a reward signal we don't have).
- No new embedding model / no re-implementation of FAISS (reuse `ComponentIndex`).
- No validator auto-construction (R2), no rhea on-demand autogen (R3), no execution-feedback loop (R6) — those are later ranks and depend on R1 existing; detailing them to snippets now would be speculation about code that doesn't exist.
- No change to the composer's prompt rules, spec mode, or skeletons beyond what name-binding requires.
- No eager pre-build of the FAISS index that violates §0 — WS1a includes a staleness guard so it rebuilds only when the corpus hash changes.

---

## 2. Workstreams

### WS1a — Turn on semantic retrieval (build artifact + config + §0 staleness guard)  ·  *S*

**Necessity:** substring search (`component_catalog.py:129-159`) misses semantically-relevant components that don't share a literal token; the FAISS path already exists and only needs enabling.

**Edit 1 — `composer_config.yml`** (after `retrieval_k`, currently `:62-64`):
```yaml
# Semantic retrieval. FAISS index built by scripts/build_rag_index.py over
# component_catalog_paths. When set, Composer._retrieve() (composer.py:406)
# uses embeddings (nanobrain ComponentIndex) instead of substring ComponentCatalog.
rag_index_dir: "rag_index"
```

**Edit 2 — §0 staleness guard** (the brutal-truth tension: a pre-built FAISS artifact is exactly what §0 warns against). `ComponentIndex` already computes `index_hash` over `(records, library_version, model_name)` (`component_index.py:165-170`). Add a lazy check at composer init (`composer.py:208-235`, the load block): if the built index's stored hash ≠ the hash of the current `component_catalog_paths`, **log loud + fall back to linear scan** (never serve a stale index silently) rather than fail-fast. Before→after of the load block's missing-files branch:
```python
# before (composer.py:223-235): missing files → raise ComposerConfigurationError
# after: missing OR stale → loud warning + linear-scan fallback (self._rag_index stays None)
if not (index_dir / "faiss.bin").is_file() or not (index_dir / "metadata.json").is_file():
    log.warning("rag_index_dir=%s missing/empty; falling back to linear scan. "
                "Run scripts/build_rag_index.py to enable semantic retrieval.", index_dir)
else:
    idx = ComponentIndex.load(index_dir)
    if idx.index_hash != _expected_corpus_hash(config):   # NEW staleness check
        log.warning("RAG index stale (corpus changed since build); falling back to linear scan.")
    else:
        self._rag_index = idx
```
*Brutal note:* this softens the current fail-fast to a degrade-loud. That is a deliberate behavior change — flag it in review. Alternative (keep fail-fast) is acceptable if the team prefers; the staleness check is the load-bearing part either way.

**Build command (operator step, not code):**
```bash
PYTHONPATH=../nanobrain:src .venv/bin/python scripts/build_rag_index.py \
  src/apecx_integration/composition/composer_config.yml
```

**Acceptance criteria (real data — the real 2-manifest corpus + real embeddings):**
- AC1a-1: after build, `rag_index/{faiss.bin,metadata.json}` exist and are non-empty (the builder's own post-save verify, `build_rag_index.py:104-119`).
- AC1a-2 (**the concrete semantic win**): `ComponentIndex.load(...).search("find regions conserved across viral strains", k=5)` ranks the real conservation/synthesis component **above** a component that shares zero query tokens — a hit substring search returns score 0 for. Compare against `ComponentCatalog.search` on the same query: assert the FAISS result includes a real component the substring result misses. (Run on the real manifests; no synthetic components.)
- AC1a-3: with `rag_index_dir` set but the dir deleted, `Composer.from_config(...)` loads and logs the fallback warning (degrade-loud), does NOT raise.

**Tests (real data, no synthetic):**
- `test_rag_index_built_from_real_manifests` — build over the real `component_catalog_paths`, assert ≥N records (N = real component count) + non-empty files. Pins AC1a-1.
- `test_semantic_beats_substring_on_real_query` — the AC1a-2 comparison on the real corpus. Pins the semantic win. *(Ollama-independent: embeddings are local sentence-transformers, not the LLM.)*
- `test_stale_or_missing_index_degrades_loud` — set `rag_index_dir` to a missing/stale dir; assert fallback + warning, no raise. Pins AC1a-3.

**Review gate (mandatory):** Phase 3 self-review (necessity: enabling existing machinery; minimality: 1 config key + 1 guard; readability: matches the existing load block; DRY: reuse `ComponentIndex.index_hash`; deleted: the fail-fast-on-missing branch, justified). `python <workspace>/.claude/scripts/review_policy_check.py composer.py`. Phase 4: **review-gate agent on the diff** — the deliberate fail-fast→degrade-loud change is the thing to adversarially check. Phase 5 commit with `Reviewed:` trailer.

---

### WS1b — Auto-cover discovered workflows in the composer corpus (manifest synthesis)  ·  *M — the real work*

**Necessity:** the composer can only reuse what's in its corpus; today that's 2 of ~30 workflows. The reasoning patterns (tdr_loop, best_of_n, review-revise, consensus, rag_e2e) are invisible to it. This is the precondition for "reuse/combine reasoning patterns."

**Brutal truth / design decision:** auto-discovery alone adds nothing — most workflows lack a `manifest.yml` with `rag_description`. The fix is to **synthesize a CatalogComponent per discovered workflow** so each whole workflow is referenceable as a sub-step. Decision: synthesize entries that present each discovered workflow as a `SubworkflowStep` bound by name (depends on WS1c), with `description` from the workflow's own description (`discover_workflows()` already carries `name` + `description`, `workflow_discovery.py`).

**Edit — new `composition/discovered_corpus.py`** (a thin adapter; reuse `discover_workflows`):
```python
def discovered_workflow_components() -> list[CatalogComponent]:
    """One CatalogComponent per discovered workflow, referenceable as a sub-step
    via SubworkflowStep(inner_workflow_name=<name>) (WS1c). Reuses discover_workflows()."""
    from apecx_integration.mcp_surface.workflow_discovery import discover_workflows
    return [
        CatalogComponent(
            id=f"subworkflow::{dw.name}", name=dw.name,
            description=dw.description or dw.name,
            class_path="nanobrain.library.steps.subworkflow_step.SubworkflowStep",
            yaml_path="",                       # name-bound, not path-bound
            examples=[f"reuse the {dw.name} workflow as a sub-step"],
        )
        for dw in discover_workflows()
    ]
```
Then merge these into the catalog/index corpus at `ComponentCatalog.from_manifests` call sites (`composer.py` init + `build_rag_index.py:build`). **Reuse, don't duplicate:** one synthesis function, called by both the linear catalog and the FAISS builder, so they never diverge (DRY — the review gate will check this).

**Acceptance criteria (real data — the real workflows on disk):**
- AC1b-1: `len(discovered_workflow_components()) == len(discover_workflows())` and includes real names `tdr_loop`, `best_of_n_loop`, `rag_e2e_synthesis` (assert by membership on the real repo).
- AC1b-2: after rebuild, `ComponentIndex.search("iteratively refine code against tests", k=5)` returns the real `tdr_loop` component (semantic match to TDR, which shares no literal token with the query).
- AC1b-3: the synthesized component's `class_path` resolves (it's the real `SubworkflowStep`) — i.e. `validate_workflow_against_framework` (`workflow_validator.py:215`) accepts a workflow that references it.

**Tests (real data):**
- `test_discovered_corpus_covers_all_real_workflows` — membership + count against the live `discover_workflows()`. Pins AC1b-1.
- `test_tdr_loop_retrievable_by_semantics` — AC1b-2 on the real corpus + rebuilt index. Pins AC1b-2.
- `test_synthesized_component_class_path_resolves` — AC1b-3 via the real validator. Pins AC1b-3.

**Review gate (mandatory):** self-review (DRY is the headline risk here — the synthesis must be called once and shared by catalog + builder, not copy-pasted); review-gate agent on the diff; `Reviewed:` trailer. **Blocked-by WS1c** for the `inner_workflow_name` the synthesized entries assume (or ship WS1b entries as path-bound first, then flip to name-bound after WS1c — sequencing note in §3).

---

### WS1c — `inner_workflow_name` binding seam on SubworkflowStep  ·  *M — the genuine new code (nanobrain)*

**Necessity:** the concrete unlock for "use/combine/reuse reasoning patterns" — today binding needs a hardcoded path; a composer can't say "wrap tdr_loop as my inner step by name."

**Framework constraints honored (nanobrain skills):** `SubworkflowStep` implements `process()` and does NOT override `execute()` (verified `subworkflow_step.py:454`); it owns its inner workflow instance (`:283-292`). Per `nanobrain-step-authoring` + `nanobrain-from-config`, the new field goes in the `*Config` model and resolution happens in `_init_from_config`. **Layering:** nanobrain must NOT import apecx — so resolution uses a config-supplied `workflow_search_paths`, not apecx's `discover_workflows`.

**Edit 1 — config field** (`subworkflow_step.py`, after `inner_workflow_builder` ~`:112`):
```python
    inner_workflow_name: Optional[str] = Field(default=None, description=
        "Resolve to a <name>/*.yml under workflow_search_paths. Mutually exclusive "
        "with inner_workflow_path and inner_workflow_builder.")
    workflow_search_paths: list[str] = Field(default_factory=list, description=
        "Dirs searched for inner_workflow_name resolution (apecx passes composition/workflows).")
```

**Edit 2 — 3-way mutual exclusion** (extend the path-XOR-builder check at `:254-269`): exactly one of `{path, builder, name}` set, else FAIL-FAST with the verbatim-style message the file already uses.

**Edit 3 — resolution branch** (`_init_from_config` ~`:283-290`): add a name branch that resolves then reuses the existing `Workflow.from_config(str(resolved))` path:
```python
elif name_str is not None:
    resolved = self._resolve_inner_workflow_name(name_str, config.workflow_search_paths)
    self._inner_workflow = Workflow.from_config(str(resolved))
    self._inner_workflow_path_resolved = resolved
```

**Edit 4 — resolver** (new, near `_resolve_inner_workflow_path` ~`:398`), FAIL-FAST with available names (the silent-miss guard both panels demanded):
```python
@staticmethod
def _resolve_inner_workflow_name(name: str, search_paths: list[str]) -> Path:
    cands = [y for d in search_paths for y in Path(d).glob(f"{name}/*.yml")]
    if not cands:
        avail = sorted({p.parent.name for d in search_paths for p in Path(d).glob("*/*.yml")})
        raise ComponentConfigurationError(
            f"FAIL-FAST: inner_workflow_name={name!r} not found under {search_paths}. "
            f"Available: {avail}")
    if len(cands) > 1:
        raise ComponentConfigurationError(f"FAIL-FAST: ambiguous inner_workflow_name={name!r}: {cands}")
    return cands[0]
```

**Edit 5 — MapSubworkflowStep parity** (`map_subworkflow_step.py:115-132`): capture `self._map_name` alongside `_map_builder_spec`/`_map_path`; in `_make_fresh_inner()` resolve by name when set. (Keeps map/recursive in lockstep.)

**Acceptance criteria (real data — real workflows, real nanobrain run):**
- AC1c-1 (deterministic, unconditional): `SubworkflowStep.from_config({inner_workflow_name:"tdr_loop", workflow_search_paths:[<real dir>]})` resolves to the real `composition/workflows/tdr_loop/*.yml`. Unknown name raises `FAIL-FAST` listing the real available names.
- AC1c-2 (**parity, Ollama-gated** — real e2e): on a real input, a workflow whose sub-step is bound by `inner_workflow_name:"rag_e2e_synthesis"` produces the same terminal output value as the equivalent `inner_workflow_path` binding (decide on the output VALUE, not status — G127). Gated on Ollama; `pytest.importorskip`/skipif.
- AC1c-3: mutual-exclusion — config with both `inner_workflow_name` and `inner_workflow_path` FAILs at init.

**Tests (real data, no synthetic):**
- `test_inner_workflow_name_resolves_real_tdr_loop` (nanobrain unit; deterministic) — AC1c-1 against the real apecx workflows dir passed as `workflow_search_paths`. Pins resolution + FAIL-FAST.
- `test_name_vs_path_binding_parity` (apecx integration; Ollama-gated) — AC1c-2 real e2e. Pins parity. *Unit-mock/integration parity: the deterministic resolution unit test + this live e2e cover the same seam (record in docstrings).*
- `test_three_way_mutual_exclusion` (nanobrain unit) — AC1c-3. Pins the guard.

**Review gate (mandatory, cross-repo):** nanobrain change lands FIRST as its own commit with nanobrain unit tests green + `Reviewed:` trailer; then the apecx side. Phase 3 self-review (necessity: the name seam; minimality: reuse the existing path-load after resolution — don't duplicate the load; readability: mirror the existing path/builder branches; DRY: one resolver, reused by MapSubworkflowStep; deleted: nothing). Phase 4 review-gate agent on each repo's diff — the **layering** (no apecx import in nanobrain) and the **FAIL-FAST silent-miss guard** are the adversarial checkpoints. Phase 5 commit each with `Reviewed:` trailer; `git show --stat HEAD` to confirm only intended files staged (commit-integrity rule — two repos, easy to cross-stage).

---

## 3. Dependency / task graph

```
WS1c (nanobrain: inner_workflow_name seam)  ──┐  lands first (cross-repo)
                                              ├─→ WS1b (synthesize name-bound corpus entries)
WS1a (turn on FAISS + staleness guard)  ──────┘        │
   (WS1a independent; needs WS1b's broadened corpus     │
    to be USEFUL, but ships/works on the 2-manifest      ▼
    corpus alone)                                   WS1a rebuild over the broadened corpus
                                                    → semantic retrieval over ALL patterns
```
- **WS1c blocks WS1b** (WS1b's synthesized entries are name-bound). Mitigation: WS1b can ship path-bound first, flip to name-bound after WS1c — but cleaner to do WS1c → WS1b.
- **WS1a is independent** but its *value* compounds after WS1b (semantic search over 30 patterns, not 2). Order: **WS1c → WS1b → WS1a-rebuild.**
- Each WS is its own `/feature` cycle (plan→implement→self-review→review-gate→commit). One concern per branch/worktree.

---

## 4. The mandatory review structure (the gap you flagged — explicit)

Every workstream above ends in the **same non-negotiable gate**, per `CLAUDE.md §5` + the `/feature` skill + the `review_gate.py` commit-msg hook:

1. **Phase 3 self-review** — answer the 5 questions (necessity / minimality / readability / DRY / deleted) honestly; run `review_policy_check.py` on changed Python.
2. **Phase 4 critical review** — invoke the **`review-gate` agent** on the diff (adversarial, not self-narration). Every FAIL / PASS-WITH-NOTES is addressed or gets a one-line justification. FAIL ⇒ not done.
3. **Phase 5 commit** — `Reviewed:` trailer recording Phases 3–4 (the commit-msg hook **blocks** a non-trivial commit without it); `git show --stat HEAD` to confirm staged files match the message.

Example trailer (WS1a):
```
Reviewed: necessity ok (config + guard only, enables existing FAISS path); minimality - 1 key
+ 1 staleness check, no new index code; readability matches the existing load block; DRY -
reuse ComponentIndex.index_hash for staleness, no second hash; deleted the fail-fast-on-missing
branch in favor of degrade-loud (flagged for review-gate).
```
This plan is non-trivial and cross-repo, so **none of WS1a/b/c may be committed without its own review-gate pass + trailer.** That is the part my earlier plans omitted; it is now load-bearing.

---

## 5. Real-data test policy (no synthetic data)

- All retrieval/discovery tests run over the **real manifests + real workflows on disk** and **real local embeddings** (sentence-transformers — not the LLM, so most are Ollama-independent and unconditional).
- The only LLM-gated test is the name/path **parity** e2e (AC1c-2), gated via `pytest.importorskip`/skipif on Ollama; it decides on the output VALUE (G127), never `status`.
- No fabricated components/workflows: a synthetic component would make the semantic-win assertion (AC1a-2) meaningless. The win must be shown on real corpus items.
- Unit-mock/integration parity (workspace rule): WS1c's deterministic resolution unit test is paired with the live parity e2e; record the pairing in each test docstring.

---

## 6. Brutal-truth risks / honest opinion
- **§0 tension is real:** a pre-built FAISS artifact is the kind of thing §0 says not to pre-build. WS1a's staleness guard (rebuild-or-degrade on corpus-hash change) is the honest reconciliation; without it, the index silently rots as WS1b/the on-demand rhea steps (§0) change the corpus. Do not skip the guard.
- **WS1b is where the effort actually is**, and it's a judgment call (synthesize whole-workflow entries vs author per-component manifests). I recommend whole-workflow synthesis because it directly serves reasoning-pattern reuse and reuses `discover_workflows`; per-component manifests are more work for marginal gain now.
- **Honest opinion on your request:** detailing R2–R9 to this depth now would be **speculation** — they edit code WS1a/b/c haven't created yet (the planner loop, the auto-validator, the on-demand synthesis hook). Writing snippets against non-existent code is the exact "guessing" failure I flagged on PDB. Detail them after R1 lands and the seams are real. I'd push back on any ask to snippet-level-detail all nine ranks up front.
- **Cross-repo footgun:** WS1c spans nanobrain + apecx; the commit-integrity rule (`git show --stat`) matters because a stray `git add -A` from the wrong worktree ships a misleading commit. Land nanobrain first, verify each diff in isolation.
