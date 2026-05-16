# Cross-benchmark sweep matrix — 2026-05-13

**Compute**: local Ollama / `mistral-nemo:latest`, T=0, single ollama runner.
**Datasets**: `nanobrain_native` n=10, `mbpp` n=20, `scicode` val n=5.
**Total wall-clock**: ~6.5 hours across this iteration's sweeps.

Raw JSON results in `_benchmark_runs/{P0,P1,P3_similarity,P3_max_power,P0_mbpp,P1_mbpp,P3_similarity_mbpp,P3_max_power_mbpp,P0_scicode,P1_scicode,P3_similarity_scicode,P3_max_power_scicode,ablations_nb,ablations_mbpp}/`.
Regenerate this summary via `PYTHONPATH=src .venv/bin/python _benchmark_runs/synthesize_results.py`.

## Main scaffold matrix

| Codegen | nanobrain-native (N) | MBPP (N) | SciCode val (N) | nb wall-time | mbpp wall-time |
|---|---|---|---|---|---|
| F17 `retrieval_grounded` | **0.80 ± 0.00** (N=4) | **0.65 ± 0.00** (N=2) | **0.00 ± 0.00** (N=2) | 179.6 s | 201.8 s |
| Item 2 `perturbed_consensus` | 0.80 ± 0.00 (N=3) | 0.65 (N=1) | 0.00 (N=1) | 553.9 s | 689.4 s |
| Item 3 `integrated_similarity` | **0.90 ± 0.00** (N=3) | **0.60 ± 0.00** (N=2) | 0.10 ± 0.20 (N=2, **non-det**) | 192.0 s | 213.1 s |
| `max_power` | 0.90 ± 0.00 (N=2) | 0.65 (N=1) | 0.20 (N=1) | 583.8 s | 773.0 s |

## Ablation matrix (5 partial-component combinations vs F17)

Tests whether ANY subset of {memreader, aggregator, memrecorder} reproduces
integrated_similarity's lift/regression. All landed at F17 baseline; only
the 3-way co-presence shifts pass@1.

### nanobrain-native n=10 ablations

| Workflow | Components added to F17 | pass@1 | Fail set vs F17 |
|---|---|---|---|
| F17 baseline | (router + drafter) | 0.80 | builder, tool_calculator |
| Ablation A | + memreader only | 0.80 | IDENTICAL to F17 |
| Ablation B | + aggregator only | 0.80 | IDENTICAL to F17 |
| Ablation C | + memrecorder only | 0.80 | IDENTICAL to F17 |
| Ablation D | + memreader + aggregator | 0.80 | IDENTICAL to F17 |
| Ablation E | + aggregator + memrecorder | 0.80 | IDENTICAL to F17 |
| `integrated_similarity` | + all three (closed loop) | **0.90** | tool_calculator recovered; only builder remains |

### MBPP n=20 ablations

| Workflow | Components added to F17 | pass@1 | Fail set |
|---|---|---|---|
| F17 baseline | (router + drafter) | 0.65 | 20, 57, 58, 59, 61, 63, 67 |
| Ablation A | + memreader only | 0.65 | IDENTICAL to F17 |
| Ablation B | + aggregator only | 0.65 | IDENTICAL to F17 |
| Ablation C | + memrecorder only | 0.65 | IDENTICAL to F17 |
| Ablation D | + memreader + aggregator | 0.65 | IDENTICAL to F17 |
| Ablation E | + aggregator + memrecorder | 0.65 | IDENTICAL to F17 |
| `integrated_similarity` | + all three (closed loop) | **0.60** | 14, 20, 57, 58, 59, 63, 65, 67 (recovers 61, breaks 14 + 65) |

## Per-problem detail (nanobrain-native)

| Problem | F17 N=4 | Item 2 N=3 | Item 3 N=3 | max_power N=2 |
|---|---|---|---|---|
| builder_two_step_uppercase_reverse | FAIL | FAIL | FAIL | FAIL |
| config_threshold_step | PASS | PASS | PASS | PASS |
| step_concat | PASS | PASS | PASS | PASS |
| step_dedupe_preserve_order | PASS | PASS | PASS | PASS |
| step_double | PASS | PASS | PASS | PASS |
| step_filter_positive | PASS | PASS | PASS | PASS |
| step_sum_list | PASS | PASS | PASS | PASS |
| step_uppercase | PASS | PASS | PASS | PASS |
| step_word_count | PASS | PASS | PASS | PASS |
| **tool_calculator** | **FAIL** | **FAIL** | **PASS** ⭐ | **PASS** ⭐ |

The single problem that distinguishes integrated_similarity's 90% from
F17's 80% is `tool_calculator`. The closed memory loop's similarity
retrieval surfaces a prior `step_*` solution as enrichment, which biases
the model toward correct class-structure output for the tool problem.

## Determinism characterization

| Codegen | Benchmark | N | Spread | Notes |
|---|---|---|---|---|
| F17 | nanobrain-native | 4 | 0.000 pp | bit-stable |
| F17 | MBPP | 2 | 0.000 pp | bit-stable |
| F17 | SciCode val | 2 | 0.000 pp | bit-stable |
| Item 2 | nanobrain-native | 3 | 0.000 pp | bit-stable |
| Item 3 | nanobrain-native | 3 | 0.000 pp | bit-stable |
| Item 3 | MBPP | 2 | 0.000 pp | bit-stable |
| Item 3 | SciCode val | 2 | **0.200 pp** | ⚠️ non-deterministic at small n |

**On SciCode non-determinism**: at n=5, the memory loop's order-sensitivity
dominates. Pass 1 (empty memory) populates differently from pass 2's
in-sweep FIFO state. A single problem flip = 20pp swing at n=5.

## Wall-time cost vs F17

| Codegen | nb wall-time × | mbpp wall-time × |
|---|---|---|
| F17 | 1.0× (baseline) | 1.0× (baseline) |
| Item 2 perturbed | **3.08×** | 3.42× |
| Item 3 integrated_similarity | **1.07×** | 1.06× |
| max_power | 3.25× | 3.83× |

**Item 3 is essentially free** (~7% overhead from the extra 3 cascade
nodes, but they run while waiting on the drafter's LLM call). Item 2
and max_power pay 3-4× wall-time for parallel LLM samples that
deterministic AST voting fails to discriminate among.

## Reproducibility commands

```bash
# Master synthesizer (regenerates this doc's summary tables from raw JSON):
PYTHONPATH=src .venv/bin/python _benchmark_runs/synthesize_results.py

# Re-run any cell:
rm -f src/apecx_integration/composition/_runtime/solution_memory.json
PYTHONPATH=src .venv/bin/python -m tests.benchmarks.cli <dataset> \
    --codegen <codegen> --limit <N> \
    --output _benchmark_runs/<group>/run<i>.json --quiet
```

Codegen choices for the CLI: `nanobrain_retrieval_grounded`,
`nanobrain_perturbed_consensus`, `nanobrain_integrated_similarity`,
`nanobrain_max_power`, `nanobrain_ablation_memreader_only`,
`nanobrain_ablation_aggregator_only`, `nanobrain_ablation_memrecorder_only`,
`nanobrain_ablation_memreader_aggregator`,
`nanobrain_ablation_aggregator_memrecorder`, plus all earlier pre-F17
codegens.
