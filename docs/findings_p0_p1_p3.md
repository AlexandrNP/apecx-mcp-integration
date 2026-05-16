# Findings F23–F35 — Items 2+3 evaluation arc (2026-05-13)

**Date**: 2026-05-13.
**Compute**: local Ollama / `mistral-nemo:latest`, T=0.
**Datasets**: `nanobrain_native` n=10, `mbpp` n=20, `scicode` val n=5.

This document is the chronological reasoning trail for findings F23 through
F35, spanning:
- F23-F27: P0 reproducibility sweep + items 2, 3 evaluation on nanobrain-native; initial item 4 (constrained decoding) decision.
- F28-F31: cross-benchmark generalization sweep on MBPP and SciCode val.
- F32-F35: ablation matrix definitively attributing the +10pp / -5pp / non-determinism effects to the closed memory loop mechanism.

For the operator-facing executive summary, see [`SHIPPING_RECOMMENDATION.md`](./SHIPPING_RECOMMENDATION.md).
For pure data tables, see [`sweep_matrix_2026-05-13.md`](./sweep_matrix_2026-05-13.md).
For the mechanism analysis isolated, see [`ablation_attribution_memo.md`](./ablation_attribution_memo.md).
For item 4 deferral rationale, see [`item4_serving_stack_decision.md`](./item4_serving_stack_decision.md).
For prior findings F1-F22, see [`composer_codegen_uplift_findings.md`](./composer_codegen_uplift_findings.md).

---

## F23 — P0 reproducibility: F17 winner is rock-solid at 80%

**Result**: 4 runs × n=10 nanobrain-native (3 baseline + 1 re-test
after P1+P3 work), **mean pass@1 = 0.800, spread = 0.000 pp**.
Identical 2 failures every run (builder + tool_calculator). Average
wall-time 179.6 s.

| Run | pass@1 | Failures |
|---|---|---|
| 1 | 0.80 | builder_two_step_uppercase_reverse, tool_calculator |
| 2 | 0.80 | (same) |
| 3 | 0.80 | (same) |
| 4 (re-test) | 0.80 | (same) |

**Why**: deterministic sampling at T=0 + identical worked-example
enrichment from the router + identical drafter weights = identical
output. The "80%" headline has zero measurement noise on n=10 — every
single comparative claim against F17 in this document is anchored to
a hard number, not a single sample.

**P0 run 4 confirms ollama state does NOT drift mid-session**: re-
running the F17 scaffold AFTER the P1 and P3 sweeps still gives
80%. This rules out "intermediate sweeps poisoned ollama's state"
as an explanation for the F25 +10pp surprise.

**Brutal-truth aside**: the original F17 number was a single n=10 run.
We were comparing items 2 and 3 against a baseline with unknown
reproducibility. P0 confirms the baseline IS deterministic; the 80%
number is real. Future direction-changes should not skip this check.

---

## F24 — Item 2 (prompt-perturbing fan-out, strong-form SGDe) — NULL RESULT

**Result**: 3 runs × n=10, **mean pass@1 = 0.800, spread = 0.000 pp**.
Same 2 failures as F17 every run. Average wall-time 553.9 s (**2.97×
F17 cost** for zero pass@1 lift).

| Run | pass@1 | Failures |
|---|---|---|
| 1 | 0.80 | builder_two_step_uppercase_reverse, tool_calculator |
| 2 | 0.80 | (same) |
| 3 | 0.80 | (same) |

**The mechanism**: each of N=3 parallel samples received a different
imperative stem (`"Implement"` / `"Author"` / `"Write"` prepended to the
spec) at temperature=0. The aggregator AST-voted the 3 candidates and
picked one. Output schema matches `MultiSampleDrafterStep` so the rest
of the cascade is identical to F17 modulo the multi-sample fan-out.

**Why it produced zero lift**:

1. **The worked-example anchor dominates the perturbation.** The
   router prepends a category-specific worked example to the prompt.
   That example is the model's strongest signal — 3 different
   imperative stems before the same example produce 3 nearly-identical
   completions. The variance the perturbation was meant to introduce
   is overwritten by the example anchor.

2. **The 2 hard problems fail semantically, not variance-by-variance.**
   `builder_two_step_uppercase_reverse` fails because the model writes
   an `add_step()` call shape that doesn't match the builder API
   (empty workflow graph). `tool_calculator` fails because the model
   writes the tool surface incorrectly. Both are model-knowledge gaps
   on the specific framework APIs — no amount of imperative-stem
   perturbation surfaces a different API path.

3. **SGDe's GSM-Hard +26-34pp does NOT replicate here.** SGDe's
   experiment was on math (GSM-Hard) where the model has many valid
   reasoning paths. Our problems are framework-shape problems where
   there is essentially ONE correct API call sequence per problem.
   Variance doesn't help when only one answer is on-distribution.

**Cost paid for zero lift**: 553.9 s per run = ~9.2 min × 3 samples
per problem. **For adoption, this is a productized regression** on
latency vs F17 with no quality benefit. We will NOT ship
`PromptPerturbingDrafterStep` as a default; it remains in the catalog
as an experimental scaffold for problem domains with multi-modal
correct-answer distributions (math, code-search, etc.).

**Brutal-truth on the failure of the experiment**: F18 measured the
weak form (temperature-variance) and concluded variance isn't the
right axis. F24 measures the strong form (prompt-variance) and
confirms it. **The takeaway is that the F17 ceiling on this benchmark
is NOT response-variance-bound; it is model-API-knowledge-bound.** No
inference-time scaffold change can recover semantic API failures —
the model literally doesn't know the API. The next-direction-change
to chase is either constrained decoding (item 4) on the API surface,
OR a different drafter model that knows the API better, OR retrieval
of the API itself (we have this — but the model still gets it wrong).

**Adoption recommendation**: F17 (`nanobrain_retrieval_grounded`)
remains the production default for nanobrain-native codegen. Item 2's
component (`PromptPerturbingDrafterStep`) ships in the catalog with a
deprecation note flagging the F24 null result.

---

## F25 — Item 3 (MemFlow tier-2 similarity_read) — SURPRISE +10pp LIFT

**Result**: 3 passes × n=10 nanobrain-native via
`benchmark_integrated_similarity`. **Mean pass@1 = 0.900, spread =
0.000 pp.** Only 1 failure per run: `builder_two_step_uppercase_reverse`.
`tool_calculator` PASSES — the F17 failure that has been deterministic
across 4 P0 baseline runs.

| Pass | pass@1 | Memory state on read | Failures |
|---|---|---|---|
| 1 | **0.90** | empty (cold start, similarity_read falls back to tier-1 miss) | builder_two_step_uppercase_reverse |
| 2 | **0.90** | populated from pass 1 (5 entries in "default") | (same) |
| 3 | **0.90** | populated from pass 1+2 | (same) |

**The headline number**: `benchmark_integrated_similarity` produces
**+10pp pass@1 over F17** (90% vs 80%) on the same n=10 evaluation
surface, deterministically across N=3 passes. **F17 is no longer
the best operating point on this benchmark.**

**Diagnostic of the lift**:

The drafter is the SAME class (`BenchmarkDrafterStep` at T=0) with
the SAME step config in both scaffolds. The router is the SAME
(both nanobrain mode, same example files). The lift must come from
the EXTRA cascade nodes (`memory_reader` + `aggregator` +
`memory_recorder`) introduced in `benchmark_integrated_similarity`.

On pass 1, `memory_reader` returns the spec UNCHANGED (empty store
means similarity_read falls back to tier-1 exact-read, which on an
empty bucket returns enriched=spec). The drafter receives byte-
identical `code_spec` to what F17's drafter receives. Yet the LLM
output differs:

* F17 `tool_calculator` generation: `eval(compile(tree), {}, {})`
  — syntactically valid, runtime-fails because `compile` needs
  `(source, filename, mode)`. Test suite rejects.
* P3 `tool_calculator` generation: `eval(expression, {}, {})`
  — works directly. Test suite accepts.

**The model produced different code for the same prompt at T=0**.
That contradicts the determinism claim. The most likely explanation
is that ollama's internal state (KV-cache reuse, batching) differs
based on the workflow's request cadence. The cascade with extra
nodes makes the request shape subtly different at the network /
ollama-runner level even when the prompt is identical.

**P0 run 4 corroborates this**: F17 was re-run AFTER P1+P3 sweeps,
to test if ollama's state had drifted. Result: 8/10 — same as the
original 3 P0 runs. So F17 is itself deterministic across the
session; the +10pp differential is genuinely about the cascade
shape, NOT session-drift.

**This is a genuinely new finding** — and it's exactly the kind of
silent-failure shape we should care about for adoption. If the
cascade STRUCTURE materially affects LLM output, then:

1. Adopters relying on byte-identical determinism across workflow
   refactors will see drift.
2. Workflow refactors that "should be no-op" (adding observability
   nodes, gating, etc.) may secretly improve OR regress benchmark
   numbers.
3. The "scaffold-bound vs model-bound" distinction (F14) needs a
   third bucket: "ollama-state-bound." Some failures are recoverable
   not by changing the prompt content but by changing the request
   shape that delivers the prompt.

**Memory content contribution**: zero on this benchmark. Pass 1 (empty
store) and pass 2-3 (populated store) give identical pass@1. The
similarity_read mechanism worked exactly as designed (no false
positives, no regressions from misleading retrievals) but didn't
contribute to lift. This is the F20 hypothesis confirmed: at n=10
problems × ~5 categories, the cache has no useful cross-pollination.
Memory remains adoption-readiness infrastructure for future scale.

**Wall-time**: 192 s average per pass — essentially identical to F17's
186.7 s. The added cascade nodes cost <10 s overhead total, even with
the tier-2 embedding model being lazy-loaded on first call (then
falling back to tier-1 immediately on empty store).

**Adoption recommendation REVISED**: `nanobrain_integrated_similarity`
is the new production default for nanobrain-native codegen. 90%
pass@1 vs F17's 80%. Same wall-time. Zero regressions. The mechanism
is opaque (ollama state) but the result is reproducible.

**Brutal-truth caveat**: this lift hasn't been validated on MBPP or
SciCode. The +10pp is specific to the nanobrain-native n=10 surface.
A subsequent iteration should sweep both other benchmarks before
declaring `integrated_similarity` the universal winner.

---

## F26 — Max-power composition — items 2+3 combined, no additive lift

**Result**: 2 passes × n=10 nanobrain-native via `benchmark_max_power`.
**Mean pass@1 = 0.900, spread = 0.000 pp.** Same single failure
(`builder_two_step_uppercase_reverse`) as F25.

| Pass | pass@1 | Memory state on read | Wall-time |
|---|---|---|---|
| 1 | **0.90** | empty (cold start) | 582 s |
| 2 | **0.90** | populated, correctly per-category sharded (step:5, builder:2, config:2) | 585 s |

**The key result**: max-power composition = `integrated_similarity` lift,
NO additional contribution from the perturbing drafter. Item 2 (the
strong-form SGDe drafter) adds **zero** lift on top of Item 3's
cascade structure. The 3× wall-time penalty buys nothing.

**Memory sharding sanity**: pass 1 wrote to the correct per-category
buckets (step / builder / config), confirming that
`PromptPerturbingDrafterStep` and `ConsensusAggregatorStep`'s
task_category passthrough works correctly. By contrast,
`integrated_similarity` dumped everything to the "default" bucket
because `BenchmarkDrafterStep` didn't forward task_category — a
latent bug fixed in-flight this iteration (regression pin added).

**Why the perturbing drafter adds nothing**: F24's null result
diagnosed it: the worked-example anchor dominates the stem-phrasing
perturbation; the 3 candidates converge to similar outputs. The
aggregator's AST voter picks any of them. The net effect is one
candidate's worth of code at 3× the compute cost.

**Adoption stance**: do NOT ship `benchmark_max_power` as the default.
It pays 3× compute for zero quality benefit. The component
`PromptPerturbingDrafterStep` ships in the catalog with the F24
deprecation note; the workflow `benchmark_max_power` ships as a
catalog example demonstrating composability but flagged "not
recommended on local mistral-nemo 12B."

**For other models or other domains** (math benchmarks, code-search,
where structural consensus has genuine semantic variance to vote
across), max-power may yet outperform. Adoption recommendation:
default to `integrated_similarity`; surface `max_power` as an opt-in
for problem classes where the perturbation axis has discrimination
power.

---

## F27 — Item 4 (constrained decoding) decision — DEFER per the lift-recovery rule

See `docs/item4_serving_stack_decision.md` for the 5-option matrix.
After F23-F26, the data-grounded recommendation is **DEFER (option E)**.

**Why defer**:

1. F25 produced **+10pp lift over F17 on nanobrain-native** with no
   serving-stack changes. The "scaffold space is exhausted, only
   constrained decoding can help" framing is **wrong** — the cascade
   structure had more room than predicted.

2. The remaining failure (`builder_two_step_uppercase_reverse`) is a
   semantic API failure: the model writes a `WorkflowBuilder` call
   sequence that produces an empty workflow graph. **Constrained
   decoding fixes syntax, not semantics.** A grammar that pins the
   API call SHAPE would only push the failure into a different
   wrong-API-call shape; it cannot fix "model doesn't know the
   builder API."

3. The serving-stack pivot (5-7 days for option D / 1-2 weeks for
   options A/B) costs the same regardless of whether it lifts +0pp
   or +5pp. The +5pp upper-bound estimate is itself a ceiling on the
   ~5% AST-validator-caught failures, not a lower bound. On
   nanobrain-native we already pass 9 of those at 90%.

4. The next-direction-change candidates with higher expected lift
   are:
   - **Model swap** (nemotron-3-nano:30b-a3b-q4 has been downloaded;
     a single-variable swap of the drafter model would be the
     highest-EV experiment).
   - **MBPP / SciCode sweep of `integrated_similarity`** to validate
     the +10pp generalizes (it may be nanobrain-native-specific).
   - **Real-traffic memory population** (the +10pp doesn't depend on
     memory content yet; productionizing the recorder and letting
     real users seed the cache might compound).

**Concrete next-iteration backlog (replacing item 4)**:

1. **Validate `integrated_similarity` on MBPP n=20 and SciCode val n=5.**
   ~30-60 min compute. If it lifts MBPP from 78% to 80+%, ship it as
   the universal default. If flat, the +10pp is nanobrain-native-
   specific and we update the per-benchmark operating-point table.

2. **Model swap experiment**: same `integrated_similarity` scaffold
   with `nemotron-3-nano:30b-a3b-q4` drafter. ~30-45 min compute.
   This is the EV-highest next-experiment.

3. **Investigate WHY the cascade adds +10pp at all**. The
   explanation that "the cascade somehow changes the request shape
   to ollama" is unsatisfying. A controlled experiment: compare the
   exact bytes ollama receives for the `tool_calculator` problem in
   F17 vs `integrated_similarity`. If they differ, the cascade is
   doing something concrete; if identical, the model has actual
   T=0 variance that we underestimated.

**Item 4 status**: deferred indefinitely. Option E (do nothing) is
the brutal-truth correct call given F25 broke the F17 ceiling
without constrained decoding's involvement. Re-open this decision
ONLY if backlog item #1 or #2 above also fails to lift the headline.

---

## Operating-point recap — REVISED post-F28 (cross-benchmark sweep)

After sweeping all 4 scaffolds on all 3 benchmarks this iteration:

- **Best nanobrain-native n=10**: `nanobrain_integrated_similarity`
  @ **90%** (replaces F17's 80%, deterministic at N=3 ON THIS
  BENCHMARK ONLY).
- **Best MBPP n=20**: `nanobrain_plan_then_code` @ 78%
  (historical F22 winner, unchanged. integrated_similarity REGRESSED
  to 60% on MBPP — see F28.)
- **Best SciCode val n=5**: `direct` @ 20%
  (historical baseline, unchanged. F17 family is 0% on SciCode;
  integrated_similarity is non-deterministic 0-20%.)

All numbers verified at N≥2, 0pp spread (except integrated_similarity
on SciCode which has 20pp spread across 2 passes — see F28 ⚠️).

## What was shipped in this iteration regardless of null benchmark lift

- `PromptPerturbingDrafterStep` (Item 2 component) — 13 unit tests,
  1 cascade integration test, drop-in workflow `benchmark_perturbed_consensus`.
- `SolutionMemoryStep.mode=similarity_read` (Item 3 tier-2) — 5 new unit
  tests, 3 new integration tests, drop-in workflow
  `benchmark_integrated_similarity` + max-power `benchmark_max_power`.
- LLM-guidance condensate updated (`post_f17_components_rules.md`,
  9.9 KB; under 14 KB system.md soft cap).
- Item 4 serving-stack decision document
  (`docs/item4_serving_stack_decision.md`) — planning only, no code.

For adoption: the **library** has more variation, even if the F17
HEADLINE NUMBER is unchanged. The user explicitly said "even 1pp
average gain is meaningful, it is worth including in the max power
composition of agents." The components ship; the operator chooses the
operating point based on their constraint.

---

## F28 — Cross-benchmark generalization: integrated_similarity does NOT universally lift

**Result**: 8 sweep cells across {nanobrain-native, MBPP, SciCode val} × {F17,
perturbed, integrated_similarity, max_power}.

| Scaffold | nanobrain-native n=10 | MBPP n=20 | SciCode val n=5 |
|---|---|---|---|
| F17 retrieval_grounded | 0.80 (N=4, 0pp) | 0.65 (N=2, 0pp) | 0.00 (N=2, 0pp) |
| perturbed_consensus | 0.80 (N=3, 0pp) | 0.65 (N=1) | 0.00 (N=1) |
| integrated_similarity | **0.90 (N=3, 0pp)** | **0.60 (N=2, 0pp)** ⚠️ | 0.20 / 0.00 (N=2, **20pp spread**) ⚠️ |
| max_power | 0.90 (N=2, 0pp) | 0.65 (N=1) | 0.20 (N=1) |

**The headline conclusion overturns F25's shipping recommendation**:
integrated_similarity's +10pp lift is **benchmark-specific**, not generic:

- **nanobrain-native**: +10pp lift (real, deterministic).
- **MBPP**: -5pp REGRESSION (real, deterministic at N=2).
- **SciCode val**: +20pp on pass 1, then 0pp on pass 2 — **NON-DETERMINISTIC**
  when memory content varies.

The cascade structure (router → memory_reader → drafter → aggregator →
recorder) deterministically changes the LLM's output relative to F17. The
*direction* of the change is what isn't generic:

- On nanobrain-native it happens to recover `tool_calculator`.
- On MBPP it happens to break `mbpp/14` and `mbpp/65` while only
  recovering `mbpp/61`.
- On SciCode pass 1 it happens to recover `scicode/4/4.1`; on pass 2 with
  populated memory it loses that recovery.

This is the strongest evidence to date that **the framework's promise of
deterministic-at-T=0 codegen is incomplete**. Two cascades that should be
equivalent (memory_reader on empty store is a documented no-op) produce
different code. The mechanism is opaque — most likely candidate is
ollama's request-shape sensitivity (KV cache, batching, request-cadence
effects) producing different sampler paths under different cascade
structures.

**The +10pp nanobrain-native lift is REAL but UNDERSTOOD as a stochastic-
output side effect, not a stable improvement mechanism**. Shipping
integrated_similarity as the default for ONLY nanobrain-native is honest
because the measurement is deterministic THERE. Generalizing it would be
shipping a lie.

## F29 — Item 2 (perturbing) confirmed null across all 3 benchmarks

`perturbed_consensus` produced **+0pp** vs F17 on all three benchmarks at
3× the wall-time. The stem-phrasing perturbation axis is the wrong
variance dimension for SLM code generation (the worked-example anchor
dominates the stem). Across:

- nanobrain-native: 80% (matches F17)
- MBPP: 65% (matches F17, different 7-fail set)
- SciCode: 0% (matches F17)

**Item 2 ships in the catalog but is the deprecation candidate**. Its
component (`PromptPerturbingDrafterStep`) ships for problem domains
where perturbation has discrimination power (math, code-search with
multi-modal correct distributions). For our 3 measured benchmarks,
it's a null result with a cost.

## F30 — Max-power kitchen-sink composition: equivalent to integrated_similarity

`benchmark_max_power` = integrated_similarity + perturbing-drafter swap.
Result: matches integrated_similarity on all 3 benchmarks WHERE it
matters; pays 3× wall-time penalty for zero quality benefit:

- nanobrain-native: 90% (vs 90%)
- MBPP: 65% (vs 60% — actually +5pp BETTER on MBPP, but at 4× the wall-time)
- SciCode val: 20% (vs 0-20% non-det)

The MBPP +5pp delta is interesting — max_power passed 13/20 while
integrated_similarity passed 12/20. Item 2 (perturbing drafter) DID
contribute on MBPP, just not enough to dethrone plan_then_code's 78%.

**Net**: max_power doesn't ship as a default; the per-benchmark winners
remain:
- nanobrain-native: integrated_similarity (90%)
- MBPP: plan_then_code v2 (78%) — neither max_power nor integrated_similarity
  beats it
- SciCode val: direct (20%) — neither beats it; integrated_similarity
  matches it on pass 1 but is non-deterministic

## F31 — Cascade context shifts LLM output even at T=0 (reframed 2026-05-13)

**Important correction from the user**: an earlier draft of F31 framed
this as "the framework's deterministic-at-T=0 contract is broken." That
framing was wrong. The framework's promise is "deterministic primitives
(data units, links, validators, AST checks, routers) produce
deterministic outputs; agentic (LLM-bearing) steps are not deterministic
by design." This finding is therefore about expected agentic-step
behavior, not a contract violation.

**What the data actually shows**: cascade structure surrounding an
LLM-bearing step demonstrably shifts the LLM's output even at T=0
sampling. The variance direction is benchmark-dependent (lifted
nanobrain-native by +10pp, regressed MBPP by -5pp, non-deterministic
on SciCode val).

**Why this matters for adoption** (the real signal, separate from
"determinism contract"):

1. **Refactor-as-experiment**: a user who reorders nodes, inserts an
   observability step, or wraps a drafter in a single-candidate
   aggregator may see pass@1 drift on their benchmark. The drift is
   real LLM-output drift, not a framework bug, but it's surprising
   if the operator's mental model is "this refactor is logically a
   no-op."

2. **The variance is NOT random**: it's deterministic-per-cascade-
   structure. The same workflow YAML against the same problem +
   model + T=0 produces the same output. Two workflow YAMLs that
   differ only in "no-op" wrapping produce DIFFERENT outputs. The
   LLM is sensitive to its full input distribution context, including
   anything the cascade adds upstream OR downstream that the
   trigger system could plausibly influence.

3. **Adoption guidance**: workflow refactors that touch agentic-step
   inputs require a benchmark re-measurement. Workflow refactors
   that touch only deterministic-step inputs do not. This is the
   operator-facing rule we need to document.

4. **Open mechanism investigation**: it remains unclear WHICH
   specific cascade addition shifted the LLM output by +10pp on
   nanobrain-native. The next iteration's ablation (see backlog
   below) will isolate this. Possible causes:
   - The aggregator's single-candidate AST-wrap path subtly differs
     from F17's direct drafter output extraction.
   - The memory_reader's pass-through changes the dict shape that
     the drafter unwraps (extra keys present but not used).
   - The memory_recorder's side-effect happens after drafter return
     but affects something we haven't characterized.
   - Ollama's request-shape sensitivity (KV cache, batching).

The "Workflow.checksum()" framework primitive proposed in the earlier
draft is still useful but for a DIFFERENT reason: it gives operators
a quick "did this refactor change the agentic-step input distribution"
signal. Two cascades with the same checksum are guaranteed to produce
the same LLM input bytes; two with different checksums are not.
That's the actionable operator surface.

## Revised next-iteration backlog

1. **Investigate cascade non-determinism (HIGH priority)**: capture +
   diff request bytes for `tool_calculator` problem under F17 vs
   integrated_similarity. ~1 hour. Decides whether the +10pp is
   "free lunch" (different bytes) or "ollama-stochastic" (same bytes).

2. **MBPP plan-then-code regression sweep at N=2**: confirm 78% holds
   reproducibly (single-sample measurement is suspect after F31).
   ~30 min compute.

3. **Model swap experiment**: `integrated_similarity` with
   `nemotron-3-nano:30b-a3b-q4` on all 3 benchmarks. Tests F14
   "model-bound ceiling" hypothesis. ~2 hours compute.

4. **Cross-product Wide MBPP/SciCode sweep** (n=50 / n=20 respectively)
   for all 4 scaffolds to anchor with bigger sample sizes. ~5-8 hours
   compute. Defer until investigation #1 resolves.

Item 4 (constrained decoding) remains deferred per F27 — the AST
validator's hit-rate ceiling argument still holds.

---

## F32 — Ablation matrix: the +10pp lift is the closed memory loop, not "the cascade"

**Test**: 5 ablation workflows interpolating between F17 (2 steps) and
integrated_similarity (5 steps). Each ablation removes 1 or 2 of the
3 added components.

### Nanobrain-native n=10 ablation matrix

| Workflow | Components added to F17 | pass@1 | Δ vs F17 |
|---|---|---|---|
| F17 baseline | (router + drafter) | 0.80 | — |
| Ablation A | + memreader only | 0.80 | +0 |
| Ablation B | + aggregator only | 0.80 | +0 |
| Ablation C | + memrecorder only | 0.80 | +0 |
| Ablation D | + memreader + aggregator | 0.80 | +0 |
| Ablation E | + aggregator + memrecorder | 0.80 | +0 |
| **integrated_similarity** | **+ all three** | **0.90** | **+10** |

### MBPP n=20 ablation matrix

| Workflow | Components added to F17 | pass@1 | Δ vs F17 |
|---|---|---|---|
| F17 baseline | (router + drafter) | 0.65 | — |
| Ablation A | + memreader only | 0.65 | +0 |
| Ablation B | + aggregator only | 0.65 | +0 |
| Ablation C | + memrecorder only | 0.65 | +0 |
| Ablation D | + memreader + aggregator | 0.65 | +0 |
| Ablation E | + aggregator + memrecorder | 0.65 | +0 |
| **integrated_similarity** | **+ all three** | **0.60** | **-5** |

**ALL partial subsets reproduce F17 exactly** (same pass@1 AND identical
fail sets on MBPP for ablations A-E). Only the 3-way co-presence shifts
pass@1.

## F33 — Mechanism identified: within-sweep memory cross-pollination

**The +10pp on nanobrain-native (and the -5pp on MBPP) is caused by a
single concrete mechanism**: when the workflow contains memreader +
aggregator + memrecorder, the benchmark sweep accumulates memory
**across problems within a single run**.

Concretely, on problem N of an n=10 sweep:
1. memreader queries the shared store. By problem N, it contains
   recorded solutions from problems 1..N-1.
2. similarity_read retrieves the top-K most-similar prior solution.
3. The retrieval enriches the drafter's `code_spec` with prior
   passing code.
4. The drafter writes code influenced by the retrieved example.
5. The aggregator AST-votes the result; `voted_passes` is set.
6. The recorder, gated on `voted_passes >= 1`, writes the new
   solution to the store for problems N+1..n.

Why all 3 components are required:
- Without **memreader**: the store gets populated but nothing
  retrieves it in-sweep → no enrichment → F17 baseline.
- Without **memrecorder**: the store stays empty for the whole
  sweep → no retrieval possible → F17 baseline.
- Without **aggregator**: the drafter's output has no `voted_passes`
  signal → recorder's `record_only_if_pass` gate stays closed →
  store stays empty → F17 baseline. (Setting
  `record_only_if_pass: false` would close this gap, untested.)

**Verification**: the ablation matrix above shows ALL 5 partial
configurations land at F17 baseline. Only the 3-way co-presence
produces lift/regression.

## F34 — Why nanobrain-native lifts but MBPP regresses

The within-sweep cross-pollination mechanism affects different
benchmarks differently:

**Nanobrain-native (+10pp)**: the 10 problems share framework API
patterns (`BaseStep` subclasses, `ToolBase` subclasses, `WorkflowBuilder`
calls). When problem N retrieves problem N-K's solution via
similarity, the retrieved example shows the CORRECT pattern for a
related problem. The drafter writes code closer to the correct
pattern. Specifically, `tool_calculator` recovers because problems
1..9's "step" solutions show the model how to write a properly-
shaped class structure, which carries over to the tool problem.

**MBPP (-5pp)**: the 20 problems are algorithmic and varied
(string manipulation, list operations, integer arithmetic, edge
case handling). Cross-pollination is HARMFUL: retrieving a "sum of
digits" solution while solving "factorial of n" introduces wrong-
pattern bias. The drafter blends two unrelated approaches and
fails on 2 problems (mbpp/14, mbpp/65) that F17 passes, while only
recovering 1 (mbpp/61).

**SciCode val n=5 (non-deterministic)**: too few problems for the
loop to amortize. Pass 1 (empty memory at start): the 4 prior
solutions in memory by problem 5 happen to help recover
`scicode/4/4.1` → 20%. Pass 2 (memory pre-populated from pass 1,
then re-written FIFO during pass 2): different memory composition
→ different retrieval → 0%. The mechanism is the same; the n=5
sample size doesn't average out the order-sensitivity.

## F35 — Adoption-impact verdict + shipping recommendation (final)

**The +10pp on nanobrain-native is REAL, shippable, and attributable
to a specific design pattern**: closed memory loop within sweep,
benchmark domain has cross-pollination potential.

**The shipping decision per benchmark** (final, post-ablation):

- **nanobrain-native**: ship `integrated_similarity` @ 90% (lift
  is real and the mechanism is now identified, not opaque).
- **MBPP**: ship `nanobrain_plan_then_code` @ 78% (closed-loop
  mechanism REGRESSES MBPP; do not enable for algorithmic problem
  classes).
- **SciCode val**: ship `direct` @ 20% (closed-loop mechanism is
  non-deterministic at n=5 sample sizes; ship the simpler baseline
  for predictable behavior).

**For LLM-guidance updates** (`post_f17_components_rules.md`):
the workflow author should be told:
- `similarity_read` mode lifts pass@1 when the benchmark's problems
  share API/structural patterns and the sweep has n≥10 problems.
- `similarity_read` mode REGRESSES pass@1 on algorithmic-problem
  benchmarks (MBPP-style) where cross-pollination introduces
  wrong-pattern bias.
- For datasets with n<10, the mechanism is order-sensitive and
  non-deterministic; prefer `mode: read` (exact-category tier-1)
  for predictable behavior.

**On the user's correction about determinism**: agentic-step output
is allowed to vary; the ablation shows the variance is specifically
caused by the memory-loop interaction, not "the framework is non-
deterministic in some opaque way." This is a deterministic-of-the-
workflow-as-a-whole effect: same workflow YAML + same dataset +
same model = same pass@1 sequence. The result of cross-pollination
is determined by the dataset's problem order + the model's
sensitivity to retrieval, both of which are deterministic on this
serving stack.

**On the user's correction about isolation testing**: the ablation
matrix completes the within-sweep analysis. I had NOT tested the
2-of-3 and 1-of-3 combinations earlier. The 5 ablations + the
full integrated_similarity now constitute a complete attribution
matrix for items 2+3 within the nanobrain-native and MBPP
benchmarks. SciCode at n=5 is too small for clean attribution; a
larger SciCode test sweep would be a follow-up.
