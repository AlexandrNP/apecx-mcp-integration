# Code-writing memory (git-tracked, append-only)

Persistent state for the self-improving code-writing workflows. Each
authoring + critique + verification cycle leaves an entry under
`reflexions/<spec_id>/<timestamp>.json`. The next compose call for the
same `spec_id` (or with overlapping `spec_keywords`) reads the most
recent K entries and feeds them to the LLM as `critique` input — same
mechanism the iterative-reflexion paper (Shinn et al., NeurIPS 2023,
arXiv:2303.11366) uses for its verbal episodic memory.

## Why git-tracked

The memory has to be **reviewable**: every entry is a small JSON
file, atomic-written, with a stable schema. Reviewers can diff a
PR and see exactly what new lesson the agent recorded. No SQL, no
vector DB; the directory IS the index. `grep -r` is the retrieval
fallback when keyword-based readers don't apply.

## Schema (memory_schema_version: 1)

```json
{
  "memory_schema_version": 1,
  "id": "2026-05-12T01-30-00Z-abc123",
  "spec_id": "fizzbuzz_v1",
  "attempt_n": 1,
  "status": "pass",
  "lesson": "Free-text lesson — what the critic said, what the verifier reported, what to do differently next time.",
  "failure_keywords": ["off_by_one", "missing_base_case"],
  "spec_keywords": ["fizzbuzz", "modulo", "string_output"],
  "created_at": "2026-05-12T01:30:00+00:00",
  "source_commit": "abc1234",
  "metadata": {
    "function_name": "fizzbuzz",
    "function_signature": "def fizzbuzz(n: int) -> str",
    "code_review_approved": true,
    "exec_succeeded": null,
    "concerns_count": 2
  }
}
```

Field rules:

- **`memory_schema_version`** is currently `1`. Bumped only on
  schema-breaking change; readers must check.
- **`spec_id`** is operator-supplied OR auto-derived from
  `function_name`. Stable across attempts for the same task.
- **`status`** ∈ `{"pass", "fail", "partial"}`.
- **`lesson`** is the load-bearing free-text field; the reader injects
  it into the LLM's prompt verbatim. Keep ≤ 500 chars; longer lessons
  blow past the model's effective attention.
- **`failure_keywords`** + **`spec_keywords`** are the retrieval
  surface. The writer derives them; the reader Jaccards them.
- **`source_commit`** is the apecx-mcp-integration repo HEAD at
  write time. Null when not in a git context (e.g. tests).

## Retrieval policy (Reflexion Ω=1–3)

`MemoryReadStep` returns the **K=3 most recent entries** for a
given `spec_id`. When no entries match `spec_id` exactly,
falls back to entries whose `spec_keywords ∩ current_keywords ≠ ∅`
sorted by recency. The Reflexion paper's Ω=1–3 cap is the bound:
larger K bloats the prompt context with stale lessons.

## Write policy

`MemoryWriteStep` writes an entry per cycle, subject to gates:

- Skip when `lesson` is < 40 characters (low-signal).
- Skip when `lesson`'s keyword-Jaccard with the newest existing
  entry for the same `spec_id` is > 0.7 (restatement of a prior
  lesson).

## Atomic-write contract

Writers use `os.replace(tmp_path, final_path)` so a crashed write
never leaves a half-file in git. The path is `reflexions/<spec_id>/<id>.json`.

## Eviction

None for now (Reflexion-paper Ω-cap keeps prompt-injection size
bounded by retrieval policy, not by storage size). Operators can
prune by hand via `git rm reflexions/<spec_id>/*` and commit; the
review surfaces the deletion just like any other diff.

## Operator commands

  - **Inspect entries**: `find memory/code_writing/reflexions -name '*.json' | xargs jq -r '.lesson'`
    Quick browse of all accumulated lessons.
  - **Count by spec_id**: `ls memory/code_writing/reflexions | xargs -I{} sh -c 'echo "$(ls memory/code_writing/reflexions/{} | wc -l) {}"'`
  - **Drop a spec_id**: `git rm -r memory/code_writing/reflexions/<spec_id>` then commit
    (this is the eviction surface — reviewable per the
    git-tracked-by-design contract).
  - **Replay**: `git log -- memory/code_writing/reflexions/<spec_id>/`
    walks every commit that touched a given spec's memory.

## Verified end-to-end (2026-05-12)

`tests/integration/test_self_improvement_against_ollama.py` exercises
the full Reflexion loop against real mistral-nemo:

  - Attempt 1 (empty memory): 15.1s wall; review_approved=True;
    memory_written=True (lesson 127 chars after format).
  - Attempt 2 (same spec_id): 17.0s wall; prior_lessons=1; critique
    threaded into CodeWriteStep prompt; memory_written=False
    (restatement skip — same lesson, gate fired correctly).

## Cross-references

- `composition/steps/memory_store.py` — the pure-Python store.
- `composition/steps/memory_read_step.py` — reader BaseStep.
- `composition/steps/memory_write_step.py` — writer BaseStep.
- `composition/workflows/code_writing/self_improving_code_writing.yml`
  — the self-improvement workflow that composes them.
- Reflexion paper: arxiv.org/abs/2303.11366.
