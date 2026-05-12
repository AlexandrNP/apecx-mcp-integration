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
