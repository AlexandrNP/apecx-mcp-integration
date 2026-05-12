# Skeleton Catalog

Pre-authored `MinimalWorkflowSpec` instances under
`composition/skeletons/`. Each file is one skeleton; the composer
loads them at init and surfaces them in the spec-mode prompt so the
LLM can pick by name.

## Shipped skeletons (2026-05-12)

| Name | Steps | Best for |
|---|---|---|
| `synthesis_pipeline` | `SynthesisContextAssemblyStep` → `RagSynthesisStep` | Cross-corpus synthesis answers ("explain X", "what does the literature say about Y"). |
| `entity_extraction_only` | `EntityExtractionStep` | Single-step NER ("extract entities from this text"). |
| `pathogen_bvbrc_match` | `EntityExtractionStep` → `EnhancedBVBRCMatchStep` | Pathogen names → BV-BRC genome ids. |
| `pubmed_only_literature_search` | `PubMedHarvesterStep` | Raw citations + abstracts, no synthesis. |
| `rag_domain_search_only` | `DomainRagSearchStep` | Top-k semantic chunks, no LLM. |
| `violin_bvbrc_context_only` | `VIOLINBVBRCContextStep` | Pure pandas lookup against VIOLIN/BV-BRC. |
| `code_write_and_review` | `CodeReflectionStep` | Generate Python code + critique it against the spec. |
| `code_write_review_and_run` | `CodeReflectionStep` → `CodeVerificationStep` | Generate + critique + run in isolated subprocess (requires `APECX_CODE_EXEC=1`). |
| `generic/reflection_skeleton` (G9 typed bindings) | `<generator>` → `<critic>` | Cross-domain reflection — bind any generator + critic step pair via `Workflow.from_skeleton`. |
| `self_improving_code_writing_workflow` (real-time) | `MemoryReadStep` → `CodeWriteStep` → `CodeReviewStep` → `MemoryWriteStep` | Reflexion-style memory loop. Each cycle reads prior lessons, generates code, critiques it, persists a new lesson to `memory/code_writing/reflexions/<spec_id>/`. Git-tracked. |

## Web-research-informed patterns the catalog does NOT yet ship (deferred)

Each of these is a real workflow pattern surveyed in 2026 RAG /
agentic-AI literature. They require step classes that do NOT exist
in the current apecx component catalog; authoring a skeleton that
references invented classes would be the exact hallucination shape
CPR exists to prevent. Filing here so a future operator can pair
each pattern with a real step authoring task.

### Reflection / self-critique pattern

Cycle: `Generate → Reflect → Refine`. The LLM produces an initial
answer; a critic (another LLM call or a tool) evaluates against
criteria; the answer is revised. See
arxiv.org/abs/2501.09136 (Agentic RAG Survey, 2026) §3.2.

**To ship**: needs a new `WorkflowOutputReviewStep` (semantic-fit
review of the workflow's output, not the workflow itself). The
APECx `WorkflowReviewer` (REVIEW-AGENT, 2026-05-12) is the
composer-level analog; a workflow-level reviewer would generalize
it.

### Multi-hop retrieval

Cycle: `Retrieve₁ → decide-if-more-needed → Retrieve₂ → Synthesize`.
The first retrieval informs the second query. See
arxiv.org/abs/2506.00054 (RAG comprehensive survey, 2026) §4.1.

**To ship**: needs a `QueryRefinementStep` that takes the first-pass
retrieval output + the original prompt and produces a refined query.

### Self-consistency / N-best voting — PARTIALLY SHIPPED (2026-05-12)

Generate `N` independent answers, vote / aggregate. See the
deeplearning.ai post on agentic design patterns.

**Shipped piece**: `MultiAnswerAggregationStep`
(`composition/steps/multi_answer_aggregation_step.py`) — pure-Python
aggregator with four strategies (most_frequent / longest / first /
concatenate). Pairs with the wrapper at
`workflows/violin_bvbrc/steps/multi_answer_aggregation.yml`.
Manifest entry under step_id `AGG`.

**Still missing for a full skeleton**: a `BroadcastStep` or
framework primitive that fans out one input to N independent
downstream invocations. `DirectLink` is 1:1. Until fan-out lands,
operators wire self-consistency by hand: N RagSynthesisStep
instances + N links + the aggregator. A skeleton wrapping that
hand-wired pattern would be 4× the YAML of the simpler skeletons
and brittle to N changes; deferred until `BroadcastStep` exists.

### Code-writing flow — SHIPPED (2026-05-12)

Cycle: `CodeWriteStep → CodeReviewStep → IsolatedPyExecStep (opt-in)`.
Single LLM round-trip per leg. Surfaces to the composer as concrete
step classes (`CodeReflectionStep` for write+review;
`CodeVerificationStep` for isolated exec); the embedding via
`SubworkflowStep` is invisible at compose time.

**Shipped primitives**:
- `CodeWriteStep` (`composition/steps/code_write_step.py`): LLM →
  Python source with AST gate, function-name gate, fence-strip.
- `CodeReviewStep` (`composition/steps/code_review_step.py`):
  structured JSON verdict; grounded-rejection gate; biased toward
  rejection.
- `IsolatedPyExecStep` (`composition/steps/isolated_py_exec_step.py`):
  subprocess-isolated exec; refuse-by-default via APECX_CODE_EXEC=1;
  scrubbed env; **NOT a security sandbox**.
- `CodeReflectionStep` (`composition/steps/code_reflection_step.py`):
  SubworkflowStep wrapping the write+review pattern.
- `CodeVerificationStep` (`composition/steps/code_verification_step.py`):
  SubworkflowStep wrapping isolated exec.

**Shipped skeletons**:
- `code_write_and_review` — single-step reflection.
- `code_write_review_and_run` — reflection + isolated exec.

**Adoption caveats** (honest):
- The composer's existing RAG matcher is biased toward biological
  prompts (manifest entries for VIOLIN/BV-BRC, PubMed, etc.). When
  a user asks "write fizzbuzz", retrieval still surfaces the
  bio-domain steps as candidates. The code-writing steps' rich
  rag_descriptions help, but a domain-router primitive would be the
  cleaner long-term fix.
- The framework's trigger-binding silent-failure shape (2026-05-12)
  blocks the end-to-end workflow YAML path; the SubworkflowStep
  itself routes inputs correctly because it uses
  `wf.process + wait_for_cascade` directly. See
  `_workspace_notes/.../session_friction_log.md` for the
  investigation trail.
- "NOT a security sandbox" is loadbearing — running LLM-authored
  code via this stack on adversarial input is unsafe. Use
  `apecx_integration.composition.docker_sandbox` (T13b, gated by
  `APECX_T13B_SANDBOX_EXECUTE=1`) for that posture.

### Self-improving code-writing with git-tracked memory — SHIPPED (2026-05-12)

The Reflexion verbal-memory pattern (Shinn et al., NeurIPS 2023,
arXiv:2303.11366) applied to Python code authoring. Three new
components + one workflow:

  * `MemoryStore` (`composition/steps/memory_store.py`) — pure-Python
    file-based, atomic-write store. One JSON file per cycle under
    `memory/code_writing/reflexions/<spec_id>/<id>.json`. Reviewable
    diffs.
  * `MemoryReadStep` (`composition/steps/memory_read_step.py`) —
    reads up to K=3 most-recent entries (default), formats them as
    a critique string ready for `CodeWriteStep`. Keyword-Jaccard
    fallback when no spec_id match. No LLM.
  * `MemoryWriteStep` (`composition/steps/memory_write_step.py`) —
    derives a lesson from the cycle's `review_verdict` (+ optional
    `exec_result`), classifies status (pass/fail/partial), and
    writes atomically. Gates: skip restatements (lesson Jaccard >
    0.7), skip lessons shorter than `min_lesson_chars`.
  * Workflow: `self_improving_code_writing.yml` — composes the four
    steps into a single-level cascade. NO `SubworkflowStep`
    nesting, so it works around the open outer-cascade gap.

How adopters use it:

  1. First run for a new `spec_id`: memory_read returns empty
     critique; LLM works from spec alone. memory_write records
     the outcome (pass / fail with concerns).
  2. Subsequent runs: memory_read injects up to 3 prior lessons
     into the prompt. If priors were failures, the LLM sees the
     mistakes to avoid.
  3. The accumulated lessons live in git; PR reviewers see exactly
     what the agent learned.

Cited papers in `memory/code_writing/README.md`:
arXiv:2303.11366 (Reflexion), arXiv:2303.17651 (SELF-REFINE),
arXiv:2305.16291 (Voyager), arXiv:2304.03442 (Generative Agents).

### Code-authoring + test-writing + verification — SHIPPED (2026-05-12)

Beyond the basic reflection pattern, the catalog now ships:

  * `TestWriteStep` (CW6) — LLM-backed pytest authoring.
  * `CodeWithTestsStep` (CW7) — embeds `code_write → test_write`.
    Output dict matches `IsolatedPyExecStep`'s input shape, so an
    outer workflow can chain `CodeWithTestsStep → CodeVerificationStep`
    for "write + tests + run-tests" in three composed steps.
  * `WorkflowAnalysisStep` (CW8) — pure-Python deterministic structural
    analyzer. No LLM. Emits a stable dict (workflow_name, steps,
    links, topology, issues). Use as: (a) input to
    `WorkflowSummarizerStep` for grounded explanations; (b) CI
    pre-flight gate; (c) debugging tool when `wf.run()` "completes"
    with unexpected outputs.
  * `WorkflowSummarizerStep` (CW9) — LLM-backed plain-English
    explainer for a domain expert. Consumes the analysis dict +
    emits Markdown with 5 required sections (default
    `require_all_sections=True`). Grounded — LLM sees only the
    analysis, not raw YAML, so structural claims can't drift.

The composer's RAG matcher picks any of these when the user's prompt
semantically aligns (e.g., "explain this workflow" → `workflow_summarize`,
"write code with tests" → `code_with_tests`).

### Cross-domain reflection — SHIPPED via G9 typed-bindings skeleton (2026-05-12)

Previously deferred, now real:
``src/apecx_integration/composition/skeletons/generic/reflection_skeleton.yml``
ships a 8-hole skeleton (4 holes per side: class, config, input_du,
output_du for both generator and critic).

Usage::

    from nanobrain.core.workflow import Workflow
    wf = Workflow.from_skeleton(
        "src/.../skeletons/generic/reflection_skeleton.yml",
        bindings={
            "generator_class":     "...CodeWriteStep",
            "generator_config":    "...code_write.yml",
            "generator_input_du":  "code_write_input",
            "generator_output_du": "code_write_output",
            "critic_class":        "...CodeReviewStep",
            "critic_config":       "...code_review.yml",
            "critic_input_du":     "code_review_input",
            "critic_output_du":    "code_review_output",
        },
    )

Swap in text-domain generator+critic steps for prose reflection,
query-refinement steps for retrieval reflection, etc. The skeleton
trusts the binding shapes; pair generator+critic classes whose I/O
contracts are compatible (generator's output dict carries everything
critic's input expects via passthrough — same pattern
`CodeWriteStep` ships).

### Cross-domain reflection — legacy deferred-to-G9 note

The current `CodeReflectionStep` hardcodes its inner workflow path
(`code_reflection_workflow.yml`), which itself names
`CodeWriteStep` + `CodeReviewStep` concretely. This is code-specific.

A *generic* reflection pattern that works across domains (code,
prose, query refinement, configurations) needs typed bindings:
the generator and critic are PARAMETERS, not hardcoded classes.

**Recipe via G9 `Workflow.from_skeleton(skeleton, bindings)`**:

```python
from nanobrain.core.workflow import Workflow
wf = Workflow.from_skeleton(
    "reflection_skeleton.yml",  # generic write→review topology
    bindings={
        "generator": CodeWriteStep,   # or TextGenStep, or QueryRefinementStep, ...
        "critic": CodeReviewStep,     # or TextReviewStep, ...
    },
)
```

The `reflection_skeleton.yml` would carry `{{generator: Step}}` /
`{{critic: Step}}` placeholders; G9's binding validator enforces
shape match. Operators who need the generic pattern build it
themselves with the bindings of their choice.

**Why deferred**: the concrete `CodeReflectionStep` covers the
code-writing case shipped today. A generic skeleton is a small
authoring task (one YAML file + a bindings test) but adds API
surface that wants its own design pass for the placeholder syntax
across step/link/trigger types. Tracked as future work; the recipe
above is sufficient for adopters who need it immediately.

## Authoring guidance for future skeletons

1. Compose only EXISTING components — read the wrapper YAML to get
   exact `input_data_units` / `output_data_units` names.
2. Test with a `_StubLLM` returning `{"skeleton": "<your_name>"}`
   and assert the expanded workflow's YAML contains the canonical
   class paths.
3. Add a row to the table above with the link topology.
4. Cite the inspiration (paper / project) when adapting an external
   pattern — helps the next operator decide whether to update or
   delete the skeleton when the source pattern evolves.
