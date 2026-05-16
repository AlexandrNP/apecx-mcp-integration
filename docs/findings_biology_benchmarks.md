# Findings F36-F39 — biology benchmark integration (2026-05-14)

**Date**: 2026-05-14.
**Scope**: integration + (where runnable) sweep of 4 biology benchmarks
requested by the user: Open-Rosalind, BixBench, BioML-bench, BioProBench.
(BioDesignBench was dropped — no source link provided.)
**Compute**: local Ollama / `mistral-nemo:latest`, T=0.

Companion docs:
* [`biology_benchmark_extension_plan.md`](./biology_benchmark_extension_plan.md) — the pre-integration request + blocker analysis.
* [`BENCHMARK_EXECUTION_LOG.md`](./BENCHMARK_EXECUTION_LOG.md) — per-run + per-problem data (auto-generated).
* [`SHIPPING_RECOMMENDATION.md`](./SHIPPING_RECOMMENDATION.md) — operator-facing summary.

## Integration status — all 4 benchmarks CONFIGURED

| Benchmark | Loader | CLI wired | Runnable? | Why |
|---|---|---|---|---|
| **Open-Rosalind** | `tests/benchmarks/datasets/open_rosalind.py` | ✅ | ✅ **RAN** | `sequence_basic` subset is pure-computation; data cloned |
| **BixBench** | `tests/benchmarks/datasets/bixbench.py` | ✅ | ❌ gated | 5.91 GB capsule download + R/Bioconductor tooling + LLM judge for 83/205 |
| **BioML-bench** | `tests/benchmarks/datasets/biomlbench.py` | ✅ | ❌ deferred | per user instruction; also needs per-task data + ML graders + Docker |
| **BioProBench** | `tests/benchmarks/datasets/bioprobench.py` | ✅ | ❌ deferred | per user instruction; QA benchmark, evaluation-contract undecided |

**Every gated/deferred loader FAILS LOUDLY** when invoked without its
prerequisites — none skip-silently. Verified by 8 smoke tests in
`tests/benchmarks/test_biology_loaders.py`.

> **2026-05-14 UPDATE**: Open-Rosalind has since been re-cast as a
> **standalone workflow-generation case using Rhea as an MCP server**
> — the honest framing for a tool-agent benchmark. See
> [`open_rosalind_rhea_standalone_case.md`](./open_rosalind_rhea_standalone_case.md).
> The codegen-adapted subset documented in F36-F39 below is KEPT as the
> SECONDARY path (it produced the real F39 finding) but is no longer
> the primary Open-Rosalind integration.

## F36 — Open-Rosalind is a tool-agent benchmark; only a codegen subset is honest

Open-Rosalind (https://github.com/maris205/open-rosalind) is a
**tool-first bio-agent benchmark** — its native scoring measures
whether an agent invokes registered tools (uniprot.search, pubmed,
alphafold) and produces evidence-grounded traces. Its published
BioBench v0/v1 scores are *process-aware*.

Our 4 codegens produce **Python code that runs against assert tests**.
They do not invoke tools or emit traces. Running them against
Open-Rosalind's native harness is a benchmark-shape mismatch.

**The honest adaptation**: only the `sequence_basic` category (8 of
49 problems in `biobench_v1`, 8 of 32 in `biobench_v0`) is
pure-computation — sequence classification, length, GC%, translation,
reverse complement. Those a Python function CAN solve without tool
access. The other 41 problems require live UniProt / PubMed /
variant-DB access and are OUT OF SCOPE for the codegen evaluation
(deliberately excluded, not silently skipped).

**Splits**: Open-Rosalind has NO train/val/test — it is a fixed-eval
benchmark ("a stable score system, not SOTA"). The loader exposes the
release files as splits: `v0` (canonical, 32 tasks / 8 codegen-able),
`v1` (49 / 8), `holdout` (30 / 8). Default = `v0`.

**Honesty contract**: numbers below are NOT comparable to
Open-Rosalind's published BioBench scores. They measure "can the
drafter write pure-computation bioinformatics code" — a real signal,
a DIFFERENT signal than the source benchmark.

## F37 — Open-Rosalind v0 sweep: the "smart" scaffolds COLLAPSE

**Result**: 6 codegens × n=8 (Open-Rosalind v0 `sequence_basic` subset).

| Codegen | pass@1 | passed/total | wall-time | passed problems |
|---|---|---|---|---|
| `direct` | **0.50** | 4/8 | 47 s | seq-02, 05, 07, 08 |
| `nanobrain_retrieval_grounded` (F17) | **0.50** | 4/8 | 107 s | seq-02, 03, 05, 08 |
| `nanobrain_perturbed_consensus` | 0.375 | 3/8 | 379 s | (3 of 8) |
| `nanobrain_max_power` | **0.00** | 0/8 | 436 s | — |
| `plan_then_code` | **0.00** | 0/8 | 139 s | — |
| `nanobrain_integrated_similarity` | **0.00** | 0/8 | 135 s | — |

**The headline**: on Open-Rosalind, the SIMPLE scaffolds (direct,
retrieval_grounded) WIN at 50%, and the "smart" scaffolds
(plan_then_code, integrated_similarity, max_power) COLLAPSE to 0%.

This INVERTS the MBPP / nanobrain-native ordering, where
plan_then_code won MBPP (78%) and integrated_similarity won
nanobrain-native (90%).

`max_power` (= integrated_similarity + perturbing drafter) also gets
0/8 — it carries the same closed memory loop and suffers the same
error-amplification collapse described in F39. `perturbed_consensus`
(fan-out, NO memory loop) lands in between at 37.5%: the fan-out adds
no lift, but without the closed loop there is no error-amplification
collapse either.

## F38 — Why plan_then_code collapses on Open-Rosalind

`plan_then_code` got 0/8. Failure mode (seq-01): the planner stage
led the model to treat the FASTA header `>tiny` as a FILENAME:

```python
def solve():
    sequence = ''.join(line[1:] for line in open('tiny.fasta') ...)
    #                                          ^^^^^^^^^^^^^^^ FileNotFoundError
```

The input is `'>tiny ATGAAACGT'` — an inline sequence. `direct` (no
planner) parsed the inline sequence correctly 4/8 times. The
planner's reasoning-depth advantage on algorithmic MBPP problems
becomes a LIABILITY on inline-input bioinformatics tasks: it
over-engineers toward file I/O that doesn't exist.

## F39 — Why integrated_similarity collapses: the closed loop AMPLIFIES errors

`nanobrain_integrated_similarity` got 0/8. Failure mode (every
problem): the model wrote

```python
header, seq = sequence.split('\n', 1)   # ValueError: not enough values to unpack
```

It assumed the FASTA input had a NEWLINE separating header from
sequence. Open-Rosalind's input is space-separated on ONE line
(`'>aa MVKVGVNGFGRIGRLVTRA'`). Every one of the 8 problems failed
the same way.

**The mechanism — and the brutal-truth adoption finding**:

The closed memory loop (memory_reader + aggregator + memory_recorder,
established in F32-F34 as the +10pp lift mechanism on nanobrain-native)
is a **variance amplifier**, not a reliable improver:

* On nanobrain-native, problem 1's solution happened to be CORRECT.
  Cross-pollination spread the correct pattern → +10pp.
* On Open-Rosalind, problem 1's solution had the `split('\n')` bug.
  The recorder wrote the buggy solution; problem 2's memory_reader
  retrieved it; problem 2 copied the bug; the recorder wrote THAT;
  and so on. **The closed loop locked in the wrong pattern for the
  entire batch.** 0/8.

This is a serious **adoption-reliability concern**. A
closed-memory-loop scaffold in production, on a problem class where
the model's early attempts are wrong, will lock in the wrong pattern
for the whole batch with no recovery. The mechanism has no
self-correction — the aggregator's AST validator passes
syntactically-valid-but-semantically-wrong code, so the bad pattern
is never gated out.

**Recommendation update**: the closed memory loop must NOT be a
default. It is a per-domain opt-in that REQUIRES validation that the
domain's early-problem solutions are reliable. F35's shipping rule
("enable when problems share patterns AND n≥10") is necessary but
NOT sufficient — Open-Rosalind problems DO share patterns (all FASTA
sequences) and that's exactly what made the failure cascade. The
corrected rule: enable the closed loop ONLY when the model's base
(direct) accuracy on the domain is already high (≥70%), so the
patterns being cross-pollinated are mostly correct.

## F40 — BixBench: configured, run genuinely blocked

BixBench (https://github.com/Future-House/BixBench,
https://huggingface.co/datasets/futurehouse/BixBench) is a
**computational-biology agentic benchmark**: 205 questions from 60
real Jupyter notebooks ("capsules"). Two native modes:

1. **Agentic** — explore capsule data, run Python/R/Bash, report a
   result. Needs Docker + the capsules.
2. **Zero-shot** — answer the question from knowledge. This is QA,
   not code generation.

**The loader is CONFIGURED** (`bixbench.py`, CLI-wired, smoke-tested,
fails loud). **The RUN is genuinely blocked** on:

* **5.91 GB capsule download** (64 zip files via `hf_hub_download`).
* **R / Bioconductor tooling** — many questions are DESeq2 /
  clusterProfiler (R-native); our sandbox is Python-only. R is on
  the system (`/usr/local/bin/R`) but the sandbox would need to be
  expanded to let candidate code shell out to Rscript.
* **biopython / scanpy** — not installed (pandas/numpy/scipy/sklearn
  are).
* **An LLM judge** for 83/205 questions (`eval_mode=llm_verifier`)
  — not wired into the subprocess sandbox.

The loader produces problems in an agentic-codegen shape ("write
`solve(data_dir)` against the capsule files") and FAILS LOUDLY when
`$APECX_BIXBENCH_CAPSULES` is unset. To unblock: download capsules +
install bio tooling + decide the sandbox-R policy. That is a separate
multi-prerequisite arc, NOT a single session — documented honestly
rather than faked.

## F41 — BioML-bench + BioProBench: configured, runs deferred per instruction

Both loaders are CONFIGURED (built, CLI-wired, smoke-tested) and
**FAIL LOUDLY** unless an explicit run-enable env var is set —
matching the user's instruction "configure the rest, but their runs
should be deferred."

* **BioML-bench** (`biomlbench.py`): agentic biomedical ML-engineering
  benchmark (24 tasks: ProteinGym DMS, Open Problems single-cell,
  Polaris/TDCommons drug discovery, Kaggle medical imaging). Reads
  the canonical task list `experiments/biomlbench_v0.1a.txt` + each
  task's `config.yaml` + `description.md`. Run gate:
  `APECX_BIOMLBENCH_RUN_ENABLED=1`. Runs also need per-task data
  download + per-task ML graders + submission-file contract + Docker.
* **BioProBench** (`bioprobench.py`): biological-protocol QA benchmark
  (4 task types — PQA / ERR / ORD / GEN; canonical `_train.json` /
  `_test.json` splits per task type). Run gate:
  `APECX_BIOPROBENCH_RUN_ENABLED=1`. Runs also need an
  evaluation-contract decision (it is QA, not codegen) + MCQ /
  text-similarity graders.

## Cross-benchmark codegen ranking (updated with Open-Rosalind)

| Codegen | nanobrain-native | MBPP | SciCode val | **Open-Rosalind v0** |
|---|---|---|---|---|
| `direct` | 0.10 | 0.64 | 0.20 | **0.50** |
| `plan_then_code` | 0.10 | **0.78** | — | **0.00** |
| `retrieval_grounded` (F17) | 0.80 | 0.65 | 0.00 | **0.50** |
| `integrated_similarity` | **0.90** | 0.60 | 0.0-0.2 | **0.00** |
| `perturbed_consensus` | 0.80 | 0.65 | 0.00 | 0.375 |
| `max_power` | 0.90 | 0.65 | 0.20 | **0.00** |

Every codegen family has at least one benchmark where it is the
WORST option. `integrated_similarity` and `max_power`: best on
nanobrain-native (0.90), worst on Open-Rosalind (0.00).
`plan_then_code`: best on MBPP (0.78), worst on Open-Rosalind
(0.00). `direct`: worst on nanobrain-native (0.10), best on
Open-Rosalind (tied, 0.50). The scaffold-task fit is the dominant
variable — exactly as F1 found at the very start of this arc, now
with 4 benchmarks of evidence instead of 1.

**The brutal-truth conclusion stands and is now STRONGER**: there is
no universal codegen winner. Each benchmark has a different optimum,
and the "smart" scaffolds that win one benchmark can score ZERO on
another. The single most important adoption finding from the
Open-Rosalind sweep is F39 — the closed memory loop amplifies errors
when the domain's base accuracy is low, and has no self-correction.

## Per-benchmark shipping recommendation (updated)

| Benchmark | Ship this | pass@1 |
|---|---|---|
| nanobrain-native | `integrated_similarity` | 0.90 |
| MBPP | `plan_then_code` | 0.78 |
| SciCode val | `direct` | 0.20 |
| **Open-Rosalind (codegen subset)** | **`direct` or `retrieval_grounded`** | **0.50** |
| BixBench | (run blocked — see F40) | — |
| BioML-bench | (deferred per instruction) | — |
| BioProBench | (deferred per instruction) | — |
