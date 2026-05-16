# Item 4 serving-stack decision — DEFER

**Status**: PLANNING ONLY — no code in this iteration.
**Audience**: operator deciding whether to swap serving stack for grammar-pinned output.
**Author**: Claude, 2026-05-13 (post P0+P1+P3 sweeps).
**Cross-reference**: papers/2510.03847v1.pdf (NVIDIA SLM Survey §IV: "guided decoding and validator-first tool use").

## TL;DR — POST-DATA UPDATE

**Recommendation: defer Item 4 indefinitely. Pick option E.**

The pre-sweep planning had a conditional: "If items 2 + 3 lifted F17 by ≥1pp,
defer Item 4." That condition fired. **F25 measured `integrated_similarity`
at 90% pass@1 vs F17's 80% — a deterministic +10pp lift, N=3, 0pp spread,
zero wall-time penalty.** The scaffold space was not exhausted after all.

The remaining single failure on nanobrain-native is
`builder_two_step_uppercase_reverse`, where the model produces a
syntactically-valid `WorkflowBuilder` call sequence that semantically
produces an empty graph. **Constrained decoding (item 4's mechanism) fixes
syntax, not semantics.** A grammar that pins the API call shape would just
shift the failure into a different wrong-API-call shape. The remaining 10%
gap is not a constrained-decoding-fixable problem.

Higher-EV next-iteration candidates (in priority order):

1. **Sweep `integrated_similarity` on MBPP n=20 and SciCode val n=5** — does
   the +10pp generalize? ~30-60 min compute. Decides whether the new
   shipping recommendation is universal or nanobrain-native-specific.
2. **Model swap to `nemotron-3-nano:30b-a3b-q4`** — already downloaded;
   single-variable swap experiment. Directly tests F14's "model-bound
   ceiling" hypothesis. ~45 min compute.
3. **Mechanism investigation** — capture and diff the exact bytes ollama
   receives for the same problem in F17 vs `integrated_similarity`. The
   +10pp mechanism is opaque; understanding it would let us replicate the
   lift without the cascade overhead.

Pre-data analysis (below) is preserved for the record. The 5-option matrix
remains accurate; only the recommendation changed once data landed.

## What "constrained decoding" actually buys us

The mechanism: at every token-sampling step, mask the logit distribution by
a grammar (BNF / regex / JSON-schema / typed AST). Tokens that would
violate the grammar get probability zero. The model literally CANNOT emit
ill-formed code.

**Theoretical ceiling on our benchmarks**: ~5pp lift.

This is the brutal-truth correction to NVIDIA's framing. Their paper
targets function-calling, where structured-output discipline is the
dominant failure mode. For OUR code-generation problem, mistral-nemo
already produces syntactically-valid Python ~95% of the time (the AST
validator confirms this). The other 5% of validator-caught syntax failures
are what constrained decoding eliminates. Beyond that:

| Failure class | Constrained decoding fixes? |
|---|---|
| Markdown fence missing | ✅ yes (grammar enforces fence) |
| Indentation drift inside fence | ⚠️ partial (grammar must encode Python's indent semantics) |
| Wrong function name | ✅ yes if grammar fixes the entry point |
| Missing imports | ✅ yes if grammar requires them |
| Wrong algorithm | ❌ NO — semantic correctness, not syntactic |
| Wrong edge-case handling | ❌ NO — semantic |
| Framework-rule violation (e.g., override execute() instead of process()) | ⚠️ partial (grammar can pin method names) |
| Hardcoded prompt instead of file ref | ❌ NO — semantic |

The dominant failures on our 3 hard nanobrain-native problems (builder,
tool_calculator) are SEMANTIC, not syntactic. **Item 4's ROI on those
problems is near-zero.**

Where item 4 DOES help: the 5% of catch-all syntactic failures across
MBPP (n=20+ problems). On MBPP at n=20 with plan_then_code@78%, lifting
the 4-5 syntactically-failing cases gets us to ~82-85%. That's the
realistic win.

## Five options

| Option | Description | Build cost | OAI-compat surface | macOS-ARM viable? | Cache-friendly? |
|---|---|---|---|---|---|
| **A. vLLM** | High-throughput Python inference engine; mature XGrammar support | 1-2 weeks | yes (OpenAI-compat API) | Metal backend exists but undermaintained; CUDA-first | yes (model server caches) |
| **B. SGLang** | Native structured-generation; growing community | 1-2 weeks | yes | CUDA-first; Metal experimental | yes |
| **C. Outlines + transformers** | Pure-Python grammar lib over HF transformers | 5-7 days | no (must wrap an OAI-compat shim) | ✅ yes (CPU + MPS) | weak (Python-side model held in memory; no cache server) |
| **D. llama.cpp direct bindings** | Bypass Ollama; use llama-cpp-python with gbnf grammar | 5-7 days | no (must build a thin OAI-compat shim OR change `_llm_factory`) | ✅ yes (Metal supported natively) | ✅ yes (same gguf files Ollama already pulled) |
| **E. Defer indefinitely** | Status quo: free-form generation + AST validator catches syntax | 0 | unchanged | unchanged | unchanged |

## Option-by-option analysis

### A — vLLM

**Pros**: industry-standard. The XGrammar integration is upstream-supported.
Mature OpenAI-compat API surface; `_llm_factory.build_chat_llm` could swap
backends with zero call-site changes if we point `base_url` at the vLLM
server.

**Cons (brutal-truth)**:
- vLLM on macOS-ARM uses the Metal backend via PyTorch. The CI matrix
  doesn't include Mac. The "supported" backend has open bugs around
  quantized model loading (gguf vs safetensors).
- Setup requires either a CUDA host OR accepting Metal limitations
  (model swap-out behavior; ~2× slower than CUDA).
- Operator pain: new daemon to manage, separate model file format
  (often requires re-downloading models as safetensors).

**When to pick A**: only if you have a CUDA host available OR are
comfortable accepting Metal-backend caveats AND want best-in-class
throughput.

### B — SGLang

**Pros**: native structured generation built around their RadixAttention
design. Cleaner API for grammars than vLLM's bolted-on XGrammar layer.

**Cons (brutal-truth)**:
- Newer than vLLM; smaller user base.
- Same Metal-backend caveat as vLLM.
- Less proven in production deployments at our scale.

**When to pick B**: if you have already built familiarity with SGLang OR
want to bet on the structured-generation-first design.

### C — Outlines + transformers

**Pros**: pure-Python, runs anywhere PyTorch runs (including MPS on
macOS-ARM). The Outlines library is the most-mature grammar primitive
(Pydantic + regex + JSON-schema all supported in one API).

**Cons (brutal-truth)**:
- We lose Ollama's caching layer entirely. The model gets loaded into
  Python process memory, ~12GB for mistral-nemo Q4_K_M. Every benchmark
  process pays the cold-start cost (~30s on first call).
- No OAI-compat API. The `_llm_factory.build_chat_llm` call would have
  to switch to an Outlines-native call path; everywhere in apecx that
  uses an OAI client (composer, mcp_surface, rag_e2e_synthesis) would
  need a parallel code path.
- Outlines doesn't support all the quantization formats Ollama gives
  us for free. Switching to it might mean dropping to Q8_0 or full-
  precision, doubling RAM use.

**When to pick C**: if portability matters more than throughput AND
you're willing to fork the `_llm_factory` for an Outlines-specific path.

### D — llama.cpp direct bindings (RECOMMENDED if proceeding)

**Pros**:
- **Same gguf files** that Ollama already manages. No re-download. No
  model format conversion. Same Q4_K_M quantization.
- **Metal-supported natively** on macOS-ARM. llama.cpp's Metal backend
  is upstream-first-class, not a port.
- gbnf grammar support has been in llama.cpp since 2023. The Python
  bindings (`llama-cpp-python`) expose it via the `grammar` kwarg on
  `create_completion`.
- Operator surface: ONE pip install, no daemon to manage (loads in-
  process like Outlines BUT with quantization preserved).

**Cons (brutal-truth)**:
- No OAI-compat API by default. We'd need a small shim in
  `_llm_factory.py` to detect "constrained decoding mode" and route
  through `llama-cpp-python` instead of the Ollama OAI client. ~50
  lines of code; clean to add behind a config flag.
- We lose Ollama's model-management UX (`ollama pull`, etc.) for the
  constrained-decoding path. Operator must remember the gguf file path.
- llama-cpp-python's GIL behavior: only one inference at a time per
  loader instance. The perturbing drafter's N=3 fan-out would
  serialize, not parallelize. We lose item-2's parallelism advantage
  in compound items 2+4.

**When to pick D**: when you want constrained decoding AT ALL on macOS-
ARM AND want to preserve the Ollama gguf assets. This is the path of
least infrastructure resistance.

### E — Defer indefinitely

**Pros**: zero cost. We keep the AST validator as the syntactic gate.

**Cons**:
- The 5% upper-bound benchmark lift is permanently unrealized.
- Adoption pitch: we can't claim "schema-guaranteed output." For
  function-calling adopters who need it, this is a deal-breaker.

**When to pick E**: when items 2 + 3 already deliver the desired lift
OR when the operator is unwilling to pay the migration cost for a
+5pp ceiling.

## Recommendation matrix

| If items 2 + 3 lifted F17 by … | Recommendation |
|---|---|
| ≥ +5pp | Defer item 4. The scaffold space had more room than we thought; pivot to items 2/3 productionization. |
| +2 to +5pp | Defer item 4 for ONE more iteration. Try a different perturbation axis (worked-example variance) OR run cross-run sweep at n=50. |
| +0 to +2pp | Option D becomes the highest-EV next move. ~5-7 days to ship. |
| Regression | Option E for now; the underlying issue is model capability, not scaffold. Re-open conversation about model swap (nemotron-3-nano:30b-a3b-q4 OR Llama-3.1 70B via a remote API). |

## What I would NOT do

* **Do not build option A or B on a Mac without a CUDA host.** The
  Metal-backend caveats compound; the 1-2 week timeline turns into 3-4.
* **Do not build a hybrid "Ollama-by-default + Outlines-when-constrained"
  path inside `_llm_factory`** without first scoping the call-site impact.
  Composer, MCP surface, and RAG e2e all touch `_llm_factory`; a partial
  swap creates two code paths to maintain.
* **Do not productionize constrained decoding without integration tests
  that exercise the AST validator AND the constrained output together.**
  We need to know whether constrained decoding makes the validator
  redundant or just a second line of defense.

## Open questions for the operator

1. Is there a CUDA host available for an experimental vLLM/SGLang
   deployment, or is the constraint "must work on the dev MacBook"?
2. What is the worst-case latency budget per problem? llama-cpp-python
   in-process loading adds ~30s on first call (cold start). Acceptable
   for batch sweeps; not acceptable for interactive use.
3. Is there a constrained-decoding adoption requirement from external
   stakeholders (function-calling guarantees, schema-pinned outputs),
   or is this purely a benchmark-lift play?
4. Are we open to swapping the drafter model entirely (nemotron-3-nano,
   Llama-3.1) as a counterfactual to constrained decoding? The two are
   substitutes, not complements, for the syntactic-correctness ceiling.

## Implementation plan (if option D is chosen)

This is a sketch — NO CODE in this iteration.

**Step 1** (1 day): Add `llama-cpp-python` to `pyproject.toml` extras.
Verify a hand-coded `create_completion(prompt, grammar=...)` call works
against the existing mistral-nemo gguf file.

**Step 2** (1-2 days): Define the grammar(s). At minimum we need:
  - A grammar for "Python code in a ```python fenced block, ending with
    `def <entry_point>(...)` at top-level."
  - A grammar for "JSON object matching `{decision: pass|fix, issues: [...]}`"
    for the reviewer step.
Test each against ~10 problems with the stub harness.

**Step 3** (1-2 days): Extend `_llm_factory.build_chat_llm` with a
`grammar` kwarg that, when set, routes through `llama-cpp-python` instead
of the OAI client. Behind a feature flag (env var `APECX_USE_LLAMA_CPP=1`)
so the default path is unchanged.

**Step 4** (1 day): Wire a new step `ConstrainedDrafterStep` that uses
the grammar-aware factory. Drop-in for `BenchmarkDrafterStep` in
`benchmark_constrained_grounded/workflow.yml`.

**Step 5** (1 day): Comparative sweep on nanobrain-native + MBPP. Measure
the realistic +0-5pp lift.

**Step 6** (0.5 day): Document the new operator path (env var, gguf path,
gbnf grammar location). Update CLAUDE.md with the new dual-serving-stack
reality.

**Total estimate**: 5-7 days, single developer.

## Why I am hedged on the +5pp number

Brutal-truth final word: I have **not measured** what fraction of failures
on the current scaffolds are purely syntactic. The "~5pp ceiling" estimate
is based on the AST validator's hit-rate across our test runs, which is a
proxy. To get a real number we would need to:

1. Run F17 winner on nanobrain-native N=20 (more data).
2. Categorize each failure: syntactic (caught by AST validator if scaffold
   used it) vs semantic (model wrote correct-shape wrong code).
3. The syntactic fraction is the upper bound on item 4's contribution.

This is a 1-2 hour analysis task that should precede the 5-7 day option D
build. **If the answer is "<2% syntactic failures," option D is wasted
engineering and option E is correct.**
