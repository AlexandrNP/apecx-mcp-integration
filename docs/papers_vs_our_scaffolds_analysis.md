# Papers comparison — published SLM-scaffolding architectures vs ours

**Source corpus**: 10 PDFs in `papers/` at the workspace root.
**Status**: 2026-05-13. Honest, no-hedging comparison.

## TL;DR

All 10 papers are about Small Language Models for agentic workloads
— the same model class we're shipping with (mistral-nemo 12B). Six
of them propose scaffold architectures that **independently converge
on the same building blocks we built this session**:

| Building block | Our scaffold | Closest published cousin |
|---|---|---|
| Deterministic classifier + per-category specialization | `TaskCategoryRouterStep` (F17) | MemFlow's "Router Agent" |
| Deterministic node replacing an LLM call ("offloading") | `CodeStructureValidatorStep`, `FrameworkComplianceRunnerStep` | SGDe's "capability offloading" |
| Compiled DAG with mixed LLM + deterministic nodes | `benchmark_*_workflow.yml` | SGDe's `θ = {G, P, C}` |
| Validator-first / grounding before answering | AST + runtime probe + ConditionalLink | NVIDIA's "validator-first tool use" |
| Multi-agent collaboration on SLM-suitable CoTs | drafter + planner + reviewer | MACoT (six-agent framework) |
| Meta-agent that auto-iterates the scaffold | (NOT shipped) | Confucius Code Agent's "build-test-improve loop" |
| Structural consensus (fan-out / multi-sample + vote) | (NOT shipped) | SGDe's "structural consensus subgraph" |
| Tiered memory with intent-aware retrieval | (NOT shipped) | MemFlow |

## Paper-by-paper

### 1. Confucius Code Agent (Meta + Harvard, Feb 2026)

`2512.10398v6.pdf` — 29 pages. SWE-Bench-Pro: **59% Resolve@1**,
beating Anthropic Claude Opus 4.5 (54%) and OpenAI GPT-5.2 (56%)
under identical model/repo/tool conditions. Their headline claim:
**"agent scaffolding, not just model capability, is also a primary
determinant of agent performance, with appropriate orchestration and
memory structures outperforming stronger models."**

Architecture (three axes):
- **AX (Agent Experience)**: cognitive workspace — context
  distillation, structured prompts, tool invocation.
- **UX (User Experience)**: transparency, controllability.
- **DX (Developer Experience)**: meta-agent build-test-improve
  loop for auto-iterating the scaffold.

**Where we align**: their "unified orchestrator with advanced
context management" is what our workflow YAML + ConditionalLink
chain does. Their "persistent note-taking system" maps to a memory
DataUnit (we haven't built one).

**Where they have more**:
- Meta-agent that auto-iterates the scaffold's prompt + structure.
  We hand-tuned our scaffolds with manual iteration; they automate.
- Long-context tool traces across repo files. SWE-Bench is
  whole-repo; our benchmarks are single-function/class.

**Where we have more**:
- They report on a much harder benchmark (SWE-Bench-Pro). Our
  benchmarks (MBPP, SciCode, nanobrain-native) are smaller.
- Their headline number is with Claude Opus 4.5 (large). On
  Claude Sonnet 4 they get 45.5%. Their lift over SWE-agent at
  the same model is +14-18pp. That's the "scaffolding amplifier"
  effect we're trying to demonstrate.

### 2. SGDe — Compiling Deterministic Structure into SLM Harnesses (Apr 2026)

`2604.17450v3.pdf` — 6 pages. **Closest architectural cousin.**
Their abstract: "compiles agentic workflows offline into discrete
execution plans θ={G, P, C} comprising a DAG topology, system
prompts, and deterministic executable code."

Substrate choices per node:
1. **Prompt refinement** — revise the system prompt.
2. **Capability offloading** — replace LLM node with deterministic
   code.
3. **Structural consensus** — replicate node with prompt
   perturbations + deterministic vote aggregation.

**Where we align (very strongly)**:
- Our nanobrain workflow YAML IS exactly the `θ = {G, P, C}`
  formulation. The YAML topology, the per-step prompt files, and
  the deterministic steps (validators, router) are SGDe's three
  components by name.
- Our `FrameworkComplianceRunnerStep` and
  `CodeStructureValidatorStep` ARE "capability offloading" by
  their definition.
- Our `TaskCategoryRouterStep` is a constrained form of "prompt
  refinement" (per-category prompt selection).

**Where they have more**:
- **Structural consensus** (multi-sample vote) — we never tried
  this. They report +26-34pp lift on GSM-Hard from this alone.
- A **teacher LLM** that generates the gradients (their teacher
  rewrites the SLM's harness). We hand-author our scaffolds.

**Where we have more (slightly)**:
- We expose the silent-failure shapes (F3, F11, F14, F15, F17)
  the framework's runtime exhibits. They don't discuss runtime
  framework gaps.
- We measured on three benchmark families; they only report on
  GSM-Hard-derived. Their +26pp claim doesn't generalize to
  our F1 ("scaffold-task fit dominates").

**Honest assessment**: SGDe is the paper we should cite if we
publish. Our independent convergence on the DAG + offloading +
prompt-refinement pattern is a strong external validation. The
piece we're missing (structural consensus) is the next experiment
to run.

### 3. MemFlow — Intent-Driven Memory Orchestration (May 2026)

`2605.03312v1.pdf` — 26 pages. SLM agents (Qwen3-1.7B) with
**Router Agent → Memory Agent (tiered) → Answer Agent → Validator
Agent**. Three memory tiers: Profile Lookup / Targeted Retrieval /
Deep Reasoning. Headline: **~2× accuracy over full-context SLM
baselines** on LongMemEval / LoCoMo / LongBench.

**Where we align**:
- Their Router Agent IS our TaskCategoryRouterStep architecturally
  — classify the query, dispatch to specialized handler. Same
  pattern, different problem domain (memory tier vs code example).
- Their Validator Agent IS our `FrameworkComplianceRunnerStep`
  pattern: grounding-validated retry.
- They explicitly call out **"route-then-compile design avoids
  tool-selection hallucination and reasoning loops"** — which is
  the same argument we make for moving classification to a
  deterministic step.

**Where they have more**:
- Real **memory** across queries. We don't persist anything
  across problems. Adoption-relevant: a real composer would
  remember which prompts produced PASS code and re-use those
  patterns. This is the natural next step.
- Their **3-tier escalation** (profile → retrieval → deep
  reasoning) maps to graceful degradation we don't yet have.

**Where we have more**: nothing specific. They independently
discovered the router pattern; we built it before reading them.

### 4. MACoT — Multi-Agent CoT Synthesis for SLMs (AAAI 2026)

`23065-AAAI26.TangG-MS.pdf` — 9 pages. Six-agent collaboration to
synthesize SLM-tunable CoTs. Headline: fine-tuning Qwen2.5-7B with
**only 1879 synthetic CoTs** matches much larger distillation
methods.

Key intuition: long CoTs from large reasoning models **hurt** SLMs
because the SLM's "limited learning capacity" can't absorb the
self-reflection content. SLM-tuned CoTs need to be **shorter and
more semantically explicit**.

**Where this informs our work**:
- Our F14 (the reviser ignores critique on small models) IS the
  same phenomenon at inference time that MACoT's training-time
  intuition captures. Long, self-reflective scaffold critiques
  overload mistral-nemo's reasoning the same way long-CoT
  distillation overloads Qwen2.5-7B's training capacity.
- Their fix is to compress the CoTs. Our F17 fix is to provide
  **worked examples** instead of critique — both shorten the
  cognitive load on the SLM.
- We don't fine-tune; they do.

**Lesson for us**: keep scaffold critiques SHORT and POSITIVE.
F17's `example_*.md` files are ~50-200 LOC each — that's the right
size for an SLM to process. The longer `nanobrain_rules.md`
condensate (4.2 KB) was approaching the boundary.

### 5. NVIDIA SLM Survey (Oct 2025)

`2510.03847v1.pdf` — 9 pages. **Position paper**: SLMs (1-12B) are
not only sufficient but often **superior** for agentic workloads
(RAG, function calling, structured decoding, programmatic tool
use). Cost/latency/energy advantages **10×-100× over LLMs**.

Their proposed architecture: **SLM-default, LLM-fallback systems
with uncertainty-aware routing and verifiers**. Engineering
metrics: Cost per Successful task (CPS), schema validity,
executable-call rate, p50/p95 latency.

**Where we align**:
- "Guided decoding and validator-first tool use allow SLMs to
  match or surpass LLMs at a 10×-100× lower token cost" — this
  is exactly the value proposition of our AST-gated /
  runtime-gated scaffolds (deterministic validator first, LLM
  only when needed).

**Where they have more**:
- **Guided decoding / constrained output**. Their argument:
  schema-constrained output (XGrammar, Outlines) makes SLMs
  reliable for function calling. We use raw LLM with markdown
  fences. Constrained decoding could lift our 70% nanobrain-
  native further by ELIMINATING the syntactic failures the AST
  validator currently catches.
- Their **engineering metrics** (CPS, schema validity, executable-
  call rate) are what we should be reporting for adoption.

**Concrete next-iteration suggestion**: integrate Outlines or
XGrammar as the drafter's output constraint for nanobrain-native
problems. Force the LLM to emit only Python that matches the
expected class signature grammar.

### 6. HarnessLLM — Test Harness Generation via RL (Nov 2025)

`2511.01104v1.pdf` — 23 pages. **Two-stage RL training pipeline**
that teaches LLMs to write *harness code* (synthesizes inputs +
validates outputs with invariant checking) instead of input-output
test pairs. Test-time scaling using their generated tests.

**Where this informs our work**:
- Our MB-1 scaffold (edge-case enumerator) is a non-RL variant of
  the same intuition: have the LLM think about test inputs
  before drafting code.
- They argue input-output pairs are insufficient for thorough
  testing. Our scaffold's "test_hint = first line of test_code"
  is exactly the failure mode they highlight.

**Where they have more**: RL training, which we explicitly
deferred (workspace policy: local-only, no fine-tuning).

### 7. AgentDistill — Training-Free Agent Distillation (Jun 2025)

`2506.14728v1.pdf` — 13 pages. **MCP Boxes** (Model-Context-
Protocols) as reusable modules a teacher agent generates and a
student agent imports. No training. Student inherits task-solving
capabilities without gradient updates.

**Where this informs our work**:
- The MCP-Box pattern maps to our reusable nanobrain components
  (`composition/steps/*.py`). We have a step library; we don't
  have a teacher that generates new steps automatically.
- "Training-free" matches our workspace policy (no fine-tuning).

**Where they have more**: their teacher agent auto-creates new
modules; we hand-author. Confucius Code Agent's meta-agent is
similar.

### 8. SWEnergy — Empirical Energy Study on SLM Agents (Dec 2025)

`2512.09543v2.pdf` — 8 pages. Four agentic frameworks (SWE-Agent,
OpenHands, Mini SWE Agent, AutoCodeRover) × two SLMs (Gemma-3-4B,
Qwen-3-1.7B) on SWE-bench Verified Mini. Headline finding:
**framework architecture is the primary driver of energy
consumption.** AutoCodeRover used 9.4× more energy than OpenHands
for near-zero task resolution.

**Where this informs our work**:
- Vindicates the "scaffold design matters" finding (F12-F16)
  empirically across multiple SLMs and frameworks.
- "Task resolution rates were near-zero (4% for AutoCodeRover)" —
  same near-zero pattern we saw with broken scaffolds (F11
  ConditionalLink-to-workflow-output silent no-op, F15 MB-1
  catastrophic regression).
- We should track ENERGY/wall-time per problem, not just pass@1,
  for adoption-pitch purposes.

### 9. Collaborative-Learning Scaffolding Simulation (Apr 2026)

`2604.11161v1.pdf` — 48 pages. LLM-based multi-agent system to
simulate collaborative learning scaffolds in education research.
Different domain; minimal direct relevance to code generation.
Skipped for detailed comparison.

### 10. Cyber-Physical SLM Agents (Aug 2025)

`v1_covered_*.pdf` — 22 pages. SLMs for Cyber-Physical Systems
control with feedback. Different domain. Skipped.

## What our session built that the papers don't have

1. **Per-task-class FILE-based curated examples** (CGU-P3-T3 +
   F17). Most papers use prompt embedding or RL. The file-based
   approach is auditable, version-controlled, no model training.
2. **Framework-specific silent-failure documentation** (F3, F11,
   F14, F15, F17). Papers don't document the framework-runtime
   shapes that bite real adopters (e.g., `wait_for_cascade` is
   settle-quiet, not request-budget).
3. **A working, reproducible benchmark harness** with deterministic
   results at temperature=0 (N=3 spread=0pp). Papers report n=1
   numbers without reproducibility analysis.

## What our session is MISSING vs the papers

In order of expected impact:

1. **Structural consensus / multi-sample voting** (SGDe). Replicate
   the drafter with prompt perturbations, vote on output via
   deterministic test pass. SGDe reports +26-34pp from this alone.
2. **Constrained decoding** (NVIDIA). Schema-constrained output via
   Outlines or XGrammar. Eliminates syntactic failure modes the
   AST validator currently catches.
3. **Meta-agent build-test-improve loop** (Confucius, AgentDistill).
   Auto-iterate the scaffold's prompts + structure. We hand-tune.
4. **Persistent cross-problem memory** (MemFlow). A memory data
   unit that accumulates "solutions that PASSED on similar
   problems" and surfaces them as additional examples.
5. **MBPP-specific worked examples** per algorithmic problem class
   (F17 generalization to MBPP, which currently routes to
   `default` and gains nothing).

The architecture is sound. The next-iteration delta is
*expansion within the same pattern*, not *re-architecting*.

## Brutal-truth conclusion

Our 14-commit, 7-scaffold session-arc independently rediscovered
the architectural building blocks that 4 of these 10 papers
formalize (SGDe, MemFlow, Confucius, NVIDIA). We don't have to
re-derive these in the next iteration; we can implement them from
the papers' designs.

The F17 breakthrough (80% nanobrain-native via per-task-class
examples) validates the published claim that **scaffold design
amplifies the SLM's effective capability**. The model isn't
ceiling-bound at 70% as F14 suggested; it was *scaffold-bound on
the validator-reviser pattern* and broke through under the
classifier-drafter-example pattern.

**For the adoption pitch**: ship `nanobrain_retrieval_grounded` as
the default codegen for nanobrain-native problems. 80% pass@1 on
local 12B mistral-nemo is competitive with what these papers
report for SLM-default systems. The remaining 20% (builder, tool)
need either structural consensus or larger drafter — both
documented next steps.

**For the next research iteration**: implement structural
consensus (SGDe-style) on the 3 hard nanobrain-native problems.
That's the single highest-expected-lift action remaining.
