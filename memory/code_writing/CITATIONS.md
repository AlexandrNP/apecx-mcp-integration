# Self-improvement pipeline — paper + project citations

The git-tracked memory pattern shipped in this directory adopts ideas
from a small set of papers and open-source projects. This file maps
each shipped feature to its primary source so future maintainers can
trace adoption decisions.

---

## Primary citations (papers)

### Reflexion (Shinn et al., NeurIPS 2023) — **PRIMARY adoption**

- **arXiv**: [arXiv:2303.11366](https://arxiv.org/abs/2303.11366)
- **Title**: "Reflexion: Language Agents with Verbal Reinforcement Learning"
- **Authors**: Noah Shinn, Federico Cassano, Edward Berman, Ashwin Gopinath, Karthik Narasimhan, Shunyu Yao
- **What we adopt**:
  - **Verbal memory** — agent stores free-text "lessons" from
    failed trajectories and reads them on subsequent attempts. Our
    `MemoryStore` writes one JSON entry per cycle with a
    free-text ``lesson`` field, mirroring the paper's `sr_t`
    self-reflection.
  - **Cap K (Ω)** — the paper bounds the episodic buffer at
    Ω=1–3 entries for programming tasks to fit LLM context.
    `MemoryReadStep` defaults to `limit=3`.
  - **Loop shape** — Actor (generate) → Evaluator (verdict) →
    Self-Reflection (lesson) → Memory append. Our
    `self_improving_code_writing.yml` is a direct mapping:
    code_write → code_review → memory_write.

### SELF-REFINE (Madaan et al., NeurIPS 2023)

- **arXiv**: [arXiv:2303.17651](https://arxiv.org/abs/2303.17651)
- **Title**: "Self-Refine: Iterative Refinement with Self-Feedback"
- **What we adopt**:
  - **Critique-template structure** — output → free-text critique
    → revised output, all from the same model. Our
    `CodeReviewStep`'s system prompt asks for structured feedback
    (concerns + suggestions) in the same single-model style.
  - **Not adopted**: persistent memory (SELF-REFINE is single-task,
    no cross-run state). Reflexion is the better template for our
    git-tracked use case.

### Voyager (Wang et al., 2023) — partial adoption (deferred)

- **arXiv**: [arXiv:2305.16291](https://arxiv.org/abs/2305.16291)
- **Title**: "Voyager: An Open-Ended Embodied Agent with Large Language Models"
- **What we adopt** (deferred — future iteration):
  - **Skill library**: `{code, description, embedding_of_description}`
    indexed for retrieval. Documented in
    `composition/skeletons/CATALOG.md` as "Pattern B" but not yet
    shipped. Reflexion's verbal-memory shape covers v1 use cases
    without requiring vector retrieval.
  - **Append-only with description-keyed retrieval** — our
    `MemoryStore` is append-only by design (file-per-cycle); the
    description-keyed retrieval is the deferred skill-library work.

### Generative Agents / memory stream (Park et al., 2023)

- **arXiv**: [arXiv:2304.03442](https://arxiv.org/abs/2304.03442)
- **Title**: "Generative Agents: Interactive Simulacra of Human Behavior"
- **What we adopt** (partial):
  - **Recency decay** as a retrieval signal — our default
    `MemoryReadStep` returns the K=3 MOST RECENT entries (Reflexion-
    style hard cap). The paper's composite recency × importance ×
    relevance score is a Pattern C documented in
    `CATALOG.md`; we ship the simpler recency-K policy first.
  - **Not adopted**: LLM-scored importance field per entry —
    introduces an extra LLM call per write, not worth the cost at
    our cadence.

### MemGPT (Packer et al., 2023)

- **arXiv**: [arXiv:2310.08560](https://arxiv.org/abs/2310.08560)
- **Title**: "MemGPT: Towards LLMs as Operating Systems"
- **What we adopt**:
  - **Restatement-skip gate** — our `MemoryWriteStep` rejects
    near-duplicate entries (lesson Jaccard > 0.7). Inspired by
    MemGPT's eviction-by-relevance idea but implemented via a
    cheap deterministic Jaccard rather than LLM-driven scoring.

### 2026 follow-up — Trajectory-Informed Memory Generation

- **arXiv**: [arXiv:2603.10600](https://arxiv.org/abs/2603.10600)
  (synthesized from 2025-2026 agent-memory survey literature)
- **What we adopt**:
  - **Failure-keyword extraction** from stderr — our
    `MemoryWriteStep._derive_failure_keywords` scans for
    `AssertionError`, `ValueError`, etc. in exec_result.stderr to
    populate `failure_keywords`. Inspired by the survey's
    "typed-tip" classification but kept lightweight.

### 2026 survey — Memory for Autonomous LLM Agents

- **arXiv**: [arXiv:2603.07670](https://arxiv.org/abs/2603.07670)
- **What we adopt**:
  - **Selective addition + selective deletion** — gates on write
    (`min_lesson_chars`, `skip_if_restatement`) prevent memory
    bloat; manual `git rm` is the eviction surface. The survey's
    finding that selective addition+deletion beats naive growth
    by ~10pp validates our gate design.

---

---

## Bug-fix workflow citations (2026-05-12 — iterative_bug_fix_workflow)

### Self-Debug (Chen et al., 2023) — **PRIMARY adoption**

- **arXiv**: [arXiv:2304.05128](https://arxiv.org/abs/2304.05128)
- **Title**: "Teaching Large Language Models to Self-Debug"
- **Quote**: "*…without any human feedback on the code correctness or
  error messages, the model is able to identify its mistakes by
  investigating the execution results and explaining the generated
  code in natural language*"
- **What we adopt**: the **explain-then-fix** protocol baked into
  `bug_fixer_system.md` — the model first names the root cause in
  its private reasoning, then patches. The "rubber duck" step
  surfaces latent reasoning so the model can spot its own bug.
- **Non-adoption**: their few-shot Codex/GPT-3.5 setup uses
  multiple example-traces in context; we run zero-shot on local
  12B mistral-nemo, accepting lower per-iteration success and
  relying on the surrounding Reflexion memory loop for cross-run
  learning.

### AutoCodeRover (Zhang et al., 2024)

- **arXiv**: [arXiv:2404.05427](https://arxiv.org/abs/2404.05427)
- **Quote**: "*Our code search exploits the program structure in
  the form of classes/methods to enhance LLM's understanding of
  the issue's root cause.*"
- **What we adopt**: **context-first** ordering in the prompt —
  the function under test + the failing assertion are presented
  BEFORE the fix request. The model sees the minimal relevant
  surface, not a fabricated whole repo.
- **Non-adoption**: their class/method graph traversal across a
  real multi-file repo. Our `bug_fix_write` operates on a single
  in-memory function snippet.

### SWE-agent (Yang et al., 2024)

- **arXiv**: [arXiv:2405.15793](https://arxiv.org/abs/2405.15793)
- **Quote**: "*SWE-agent's custom agent-computer interface (ACI)
  significantly enhances an agent's ability to create and edit
  code files, navigate entire repositories, and execute tests and
  other programs.*"
- **What we adopt**: the **verification gate** discipline. The
  fix is only accepted if `IsolatedPyExecStep` reports
  `exec_succeeded=True` on the test assertion. Patches are never
  trusted on the model's word.
- **Non-adoption**: their open/scroll/find_file/edit action set
  and repo navigation — overkill for a single-function snippet;
  would burn our 30-120s budget.

### CodeR (Chen et al., 2024) — deferred

- **arXiv**: [arXiv:2406.01304](https://arxiv.org/abs/2406.01304)
- **Quote**: "*CodeR adopts a multi-agent framework and pre-defined
  task graphs to Repair & Resolve reported bugs.*"
- **What we'd adopt** (future iteration): role separation into
  `ReproducerStep` → `LocatorStep` → `EditorStep`. v1 collapses to
  one step (bug_fix_write) because 4 separate LLM calls per fix
  exceed our budget on local 12B.

---

## Documentation workflow citations (2026-05-12 — code_documentation_workflow)

### DocAgent (Yang et al., 2025) — **PRIMARY adoption**

- **arXiv**: [arXiv:2504.08725](https://arxiv.org/abs/2504.08725)
- **Quote**: "*…a multi-faceted evaluation framework assessing
  Completeness, Helpfulness, and Truthfulness.*"
- **What we adopt**: the **three-criterion rubric**
  (Completeness, Helpfulness, Truthfulness) baked directly into
  `code_documenter_system.md`. Also surfaced in `CodeReviewStep`
  prompt for the rubric review pass.
- **Non-adoption**: the 5-agent decomposition (Reader / Searcher /
  Writer / Verifier / Orchestrator) and topological code
  traversal. We document one function at a time; the multi-agent
  overhead isn't justified.

### Khan et al. 2023 — Comparative Analysis

- **arXiv**: [arXiv:2312.10349](https://arxiv.org/abs/2312.10349)
- **Quote**: "*Closed-source models GPT-3.5, GPT-4, and Bard
  exhibit superior performance … compared to open-source LLMs,
  namely Llama 2 and StarChat.*"
- **What we adopt**: the **six-criterion checklist** (Accuracy,
  Completeness, Relevance, Understandability, Readability,
  Conciseness) referenced in our documenter prompt. Defensible
  scoring sheet that maps cleanly onto a reviewer rubric.
- **Non-adoption**: their conclusion that closed APIs win on this
  task. We deliberately accept the open-source quality gap and
  amortize it via the Reflexion memory store + multiple
  iterations.

### ShortenDoc (2024)

- **arXiv**: [arXiv:2410.22793](https://arxiv.org/abs/2410.22793)
- **What we adopt**: **terseness preference** — 3-6 line
  docstrings for simple functions, no verbose narrative.
  Documented in our prompt + matched to mistral-nemo's stronger
  short-form output.

---

## Honest non-adoptions

We deliberately did NOT adopt:

- **Vector retrieval** (Voyager skill-library style). Our memory dir
  is small (~K entries per spec_id), keyword Jaccard is fast +
  deterministic + reviewable in diffs. A vector DB introduces an
  external dependency for marginal gains.
- **LLM-scored importance/relevance** (Park-style memory stream).
  Per-write LLM calls cost wall time + dollars; the deterministic
  Jaccard-recency-K policy works for our cadence without it.
- **Episodic-buffer eviction** (Reflexion's Ω cap as a write-time
  policy). We instead cap at READ time (`limit=3`); old entries
  remain in git history and can be reactivated by raising the
  read-time limit.
- **Skill-library retrieval** (Voyager). Deferred to a v2; the
  scaffolding is documented in CATALOG.md so a future iteration
  can add it without breaking the current shape.

---

## Cross-references

- `memory/code_writing/README.md` — operator-facing schema + retrieval policy.
- `composition/skeletons/CATALOG.md` — Pattern A / B / C documentation
  + adoption rationale.
- `composition/steps/memory_store.py` — `MemoryStore` implementation.
- `composition/steps/memory_read_step.py` — `MemoryReadStep`.
- `composition/steps/memory_write_step.py` — `MemoryWriteStep`.
- `tests/integration/test_self_improvement_against_ollama.py` —
  end-to-end Reflexion-loop demonstration against real Ollama.
