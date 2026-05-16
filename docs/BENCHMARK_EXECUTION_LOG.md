# Benchmark execution log — 2026-05-13

**Compute**: local Ollama / `mistral-nemo:latest`, T=0, single-process runner.
**Total runs recorded**: 40 JSON result files across the directory tree.

This file is **auto-generated** by `_benchmark_runs/generate_execution_log.py` from every `*.json` under `_benchmark_runs/`. To regenerate after a fresh sweep:

```bash
PYTHONPATH=src .venv/bin/python _benchmark_runs/generate_execution_log.py > docs/BENCHMARK_EXECUTION_LOG.md
```

## Summary across all sweeps

| dataset | codegen | N runs | mean pass@1 | spread (pp) | avg elapsed (s) |
|---|---|---|---|---|---|
| mbpp | `nanobrain_ablation_aggregator_memrecorder` | 1 | 0.650 | 0.0 | 195.9 |
| mbpp | `nanobrain_ablation_aggregator_only` | 1 | 0.650 | 0.0 | 200.5 |
| mbpp | `nanobrain_ablation_memreader_aggregator` | 1 | 0.650 | 0.0 | 199.3 |
| mbpp | `nanobrain_ablation_memreader_only` | 1 | 0.650 | 0.0 | 196.2 |
| mbpp | `nanobrain_ablation_memrecorder_only` | 1 | 0.650 | 0.0 | 195.7 |
| mbpp | `nanobrain_integrated_similarity` | 2 | 0.600 | 0.0 | 213.1 |
| mbpp | `nanobrain_max_power` | 1 | 0.650 | 0.0 | 773.0 |
| mbpp | `nanobrain_perturbed_consensus` | 1 | 0.650 | 0.0 | 689.4 |
| mbpp | `nanobrain_retrieval_grounded` | 2 | 0.650 | 0.0 | 201.8 |
| nanobrain_native | `nanobrain_ablation_aggregator_memrecorder` | 1 | 0.800 | 0.0 | 148.9 |
| nanobrain_native | `nanobrain_ablation_aggregator_only` | 1 | 0.800 | 0.0 | 155.6 |
| nanobrain_native | `nanobrain_ablation_memreader_aggregator` | 1 | 0.800 | 0.0 | 147.7 |
| nanobrain_native | `nanobrain_ablation_memreader_only` | 1 | 0.800 | 0.0 | 169.8 |
| nanobrain_native | `nanobrain_ablation_memrecorder_only` | 1 | 0.800 | 0.0 | 153.7 |
| nanobrain_native | `nanobrain_integrated_similarity` | 3 | 0.900 | 0.0 | 192.0 |
| nanobrain_native | `nanobrain_max_power` | 2 | 0.900 | 0.0 | 583.8 |
| nanobrain_native | `nanobrain_perturbed_consensus` | 3 | 0.800 | 0.0 | 553.9 |
| nanobrain_native | `nanobrain_retrieval_grounded` | 4 | 0.800 | 0.0 | 179.6 |
| open_rosalind | `direct` | 1 | 0.500 | 0.0 | 47.0 |
| open_rosalind | `nanobrain_integrated_similarity` | 1 | 0.000 | 0.0 | 135.4 |
| open_rosalind | `nanobrain_max_power` | 1 | 0.000 | 0.0 | 435.5 |
| open_rosalind | `nanobrain_perturbed_consensus` | 1 | 0.375 | 0.0 | 378.7 |
| open_rosalind | `nanobrain_retrieval_grounded` | 1 | 0.500 | 0.0 | 107.2 |
| open_rosalind | `plan_then_code` | 1 | 0.000 | 0.0 | 138.5 |
| scicode | `nanobrain_integrated_similarity` | 2 | 0.100 | 20.0 | 181.6 |
| scicode | `nanobrain_max_power` | 1 | 0.200 | 0.0 | 588.5 |
| scicode | `nanobrain_perturbed_consensus` | 1 | 0.000 | 0.0 | 563.5 |
| scicode | `nanobrain_retrieval_grounded` | 2 | 0.000 | 0.0 | 148.0 |

---

# Detailed per-group results

## nanobrain_native — F17 retrieval_grounded baseline

Directory: `_benchmark_runs/P0/`

### Run-level summary

| Run file | codegen | pass@1 | passed/total | elapsed (s) |
|---|---|---|---|---|
| `run1.json` | `nanobrain_retrieval_grounded` | 0.80 | 8/10 | 221.3 |
| `run2.json` | `nanobrain_retrieval_grounded` | 0.80 | 8/10 | 169.7 |
| `run3.json` | `nanobrain_retrieval_grounded` | 0.80 | 8/10 | 169.2 |
| `run4.json` | `nanobrain_retrieval_grounded` | 0.80 | 8/10 | 158.2 |

### Per-problem matrix

| Problem | run1 | run2 | run3 | run4 |
|---|---|---|---|---|
| builder_two_step_uppercase_reverse | ❌ `NonZeroExit` | ❌ `NonZeroExit` | ❌ `NonZeroExit` | ❌ `NonZeroExit` |
| config_threshold_step | ✅ | ✅ | ✅ | ✅ |
| step_concat | ✅ | ✅ | ✅ | ✅ |
| step_dedupe_preserve_order | ✅ | ✅ | ✅ | ✅ |
| step_double | ✅ | ✅ | ✅ | ✅ |
| step_filter_positive | ✅ | ✅ | ✅ | ✅ |
| step_sum_list | ✅ | ✅ | ✅ | ✅ |
| step_uppercase | ✅ | ✅ | ✅ | ✅ |
| step_word_count | ✅ | ✅ | ✅ | ✅ |
| tool_calculator | ❌ `TypeError` | ❌ `TypeError` | ❌ `TypeError` | ❌ `TypeError` |


## nanobrain_native — Item 2 perturbed_consensus

Directory: `_benchmark_runs/P1/`

### Run-level summary

| Run file | codegen | pass@1 | passed/total | elapsed (s) |
|---|---|---|---|---|
| `run1.json` | `nanobrain_perturbed_consensus` | 0.80 | 8/10 | 576.7 |
| `run2.json` | `nanobrain_perturbed_consensus` | 0.80 | 8/10 | 577.6 |
| `run3.json` | `nanobrain_perturbed_consensus` | 0.80 | 8/10 | 507.5 |

### Per-problem matrix

| Problem | run1 | run2 | run3 |
|---|---|---|---|
| builder_two_step_uppercase_reverse | ❌ `NonZeroExit` | ❌ `NonZeroExit` | ❌ `NonZeroExit` |
| config_threshold_step | ✅ | ✅ | ✅ |
| step_concat | ✅ | ✅ | ✅ |
| step_dedupe_preserve_order | ✅ | ✅ | ✅ |
| step_double | ✅ | ✅ | ✅ |
| step_filter_positive | ✅ | ✅ | ✅ |
| step_sum_list | ✅ | ✅ | ✅ |
| step_uppercase | ✅ | ✅ | ✅ |
| step_word_count | ✅ | ✅ | ✅ |
| tool_calculator | ❌ `TypeError` | ❌ `TypeError` | ❌ `TypeError` |


## nanobrain_native — Item 3 integrated_similarity

Directory: `_benchmark_runs/P3_similarity/`

### Run-level summary

| Run file | codegen | pass@1 | passed/total | elapsed (s) |
|---|---|---|---|---|
| `run_pass1.json` | `nanobrain_integrated_similarity` | 0.90 | 9/10 | 197.5 |
| `run_pass2.json` | `nanobrain_integrated_similarity` | 0.90 | 9/10 | 197.3 |
| `run_pass3.json` | `nanobrain_integrated_similarity` | 0.90 | 9/10 | 181.3 |

### Per-problem matrix

| Problem | run_pass1 | run_pass2 | run_pass3 |
|---|---|---|---|
| builder_two_step_uppercase_reverse | ❌ `NonZeroExit` | ❌ `NonZeroExit` | ❌ `NonZeroExit` |
| config_threshold_step | ✅ | ✅ | ✅ |
| step_concat | ✅ | ✅ | ✅ |
| step_dedupe_preserve_order | ✅ | ✅ | ✅ |
| step_double | ✅ | ✅ | ✅ |
| step_filter_positive | ✅ | ✅ | ✅ |
| step_sum_list | ✅ | ✅ | ✅ |
| step_uppercase | ✅ | ✅ | ✅ |
| step_word_count | ✅ | ✅ | ✅ |
| tool_calculator | ✅ | ✅ | ✅ |


## nanobrain_native — max_power

Directory: `_benchmark_runs/P3_max_power/`

### Run-level summary

| Run file | codegen | pass@1 | passed/total | elapsed (s) |
|---|---|---|---|---|
| `run_pass1.json` | `nanobrain_max_power` | 0.90 | 9/10 | 582.5 |
| `run_pass2.json` | `nanobrain_max_power` | 0.90 | 9/10 | 585.0 |

### Per-problem matrix

| Problem | run_pass1 | run_pass2 |
|---|---|---|
| builder_two_step_uppercase_reverse | ❌ `NonZeroExit` | ❌ `NonZeroExit` |
| config_threshold_step | ✅ | ✅ |
| step_concat | ✅ | ✅ |
| step_dedupe_preserve_order | ✅ | ✅ |
| step_double | ✅ | ✅ |
| step_filter_positive | ✅ | ✅ |
| step_sum_list | ✅ | ✅ |
| step_uppercase | ✅ | ✅ |
| step_word_count | ✅ | ✅ |
| tool_calculator | ✅ | ✅ |


## nanobrain_native — ablation matrix

Directory: `_benchmark_runs/ablations_nb/`

### Run-level summary

| Run file | codegen | pass@1 | passed/total | elapsed (s) |
|---|---|---|---|---|
| `aggregator_memrecorder_r1.json` | `nanobrain_ablation_aggregator_memrecorder` | 0.80 | 8/10 | 148.9 |
| `aggregator_only_r1.json` | `nanobrain_ablation_aggregator_only` | 0.80 | 8/10 | 155.6 |
| `memreader_aggregator_r1.json` | `nanobrain_ablation_memreader_aggregator` | 0.80 | 8/10 | 147.7 |
| `memreader_only_r1.json` | `nanobrain_ablation_memreader_only` | 0.80 | 8/10 | 169.8 |
| `memrecorder_only_r1.json` | `nanobrain_ablation_memrecorder_only` | 0.80 | 8/10 | 153.7 |

### Per-problem matrix

| Problem | aggregator_memrecorder_r1 | aggregator_only_r1 | memreader_aggregator_r1 | memreader_only_r1 | memrecorder_only_r1 |
|---|---|---|---|---|---|
| builder_two_step_uppercase_reverse | ❌ `NonZeroExit` | ❌ `NonZeroExit` | ❌ `NonZeroExit` | ❌ `NonZeroExit` | ❌ `NonZeroExit` |
| config_threshold_step | ✅ | ✅ | ✅ | ✅ | ✅ |
| step_concat | ✅ | ✅ | ✅ | ✅ | ✅ |
| step_dedupe_preserve_order | ✅ | ✅ | ✅ | ✅ | ✅ |
| step_double | ✅ | ✅ | ✅ | ✅ | ✅ |
| step_filter_positive | ✅ | ✅ | ✅ | ✅ | ✅ |
| step_sum_list | ✅ | ✅ | ✅ | ✅ | ✅ |
| step_uppercase | ✅ | ✅ | ✅ | ✅ | ✅ |
| step_word_count | ✅ | ✅ | ✅ | ✅ | ✅ |
| tool_calculator | ❌ `TypeError` | ❌ `TypeError` | ❌ `TypeError` | ❌ `TypeError` | ❌ `TypeError` |


## MBPP — F17 retrieval_grounded baseline

Directory: `_benchmark_runs/P0_mbpp/`

### Run-level summary

| Run file | codegen | pass@1 | passed/total | elapsed (s) |
|---|---|---|---|---|
| `run1.json` | `nanobrain_retrieval_grounded` | 0.65 | 13/20 | 209.7 |
| `run2.json` | `nanobrain_retrieval_grounded` | 0.65 | 13/20 | 194.0 |

### Per-problem matrix

| Problem | run1 | run2 |
|---|---|---|
| 11 | ✅ | ✅ |
| 12 | ✅ | ✅ |
| 14 | ✅ | ✅ |
| 16 | ✅ | ✅ |
| 17 | ✅ | ✅ |
| 18 | ✅ | ✅ |
| 19 | ✅ | ✅ |
| 20 | ❌ `AssertionError` | ❌ `AssertionError` |
| 56 | ✅ | ✅ |
| 57 | ❌ `NameError` | ❌ `NameError` |
| 58 | ❌ `NameError` | ❌ `NameError` |
| 59 | ❌ `AssertionError` | ❌ `AssertionError` |
| 61 | ❌ `AssertionError` | ❌ `AssertionError` |
| 62 | ✅ | ✅ |
| 63 | ❌ `AssertionError` | ❌ `AssertionError` |
| 64 | ✅ | ✅ |
| 65 | ✅ | ✅ |
| 66 | ✅ | ✅ |
| 67 | ❌ `Timeout` | ❌ `Timeout` |
| 68 | ✅ | ✅ |


## MBPP — Item 2 perturbed_consensus

Directory: `_benchmark_runs/P1_mbpp/`

### Run-level summary

| Run file | codegen | pass@1 | passed/total | elapsed (s) |
|---|---|---|---|---|
| `run1.json` | `nanobrain_perturbed_consensus` | 0.65 | 13/20 | 689.4 |

### Per-problem matrix

| Problem | run1 |
|---|---|
| 11 | ✅ |
| 12 | ✅ |
| 14 | ✅ |
| 16 | ❌ `AssertionError` |
| 17 | ✅ |
| 18 | ✅ |
| 19 | ✅ |
| 20 | ❌ `AssertionError` |
| 56 | ✅ |
| 57 | ❌ `NameError` |
| 58 | ❌ `NameError` |
| 59 | ❌ `AssertionError` |
| 61 | ✅ |
| 62 | ✅ |
| 63 | ❌ `AssertionError` |
| 64 | ✅ |
| 65 | ✅ |
| 66 | ✅ |
| 67 | ❌ `Timeout` |
| 68 | ✅ |


## MBPP — Item 3 integrated_similarity

Directory: `_benchmark_runs/P3_similarity_mbpp/`

### Run-level summary

| Run file | codegen | pass@1 | passed/total | elapsed (s) |
|---|---|---|---|---|
| `run_pass1.json` | `nanobrain_integrated_similarity` | 0.60 | 12/20 | 214.4 |
| `run_pass2.json` | `nanobrain_integrated_similarity` | 0.60 | 12/20 | 211.7 |

### Per-problem matrix

| Problem | run_pass1 | run_pass2 |
|---|---|---|
| 11 | ✅ | ✅ |
| 12 | ✅ | ✅ |
| 14 | ❌ `AssertionError` | ❌ `AssertionError` |
| 16 | ✅ | ✅ |
| 17 | ✅ | ✅ |
| 18 | ✅ | ✅ |
| 19 | ✅ | ✅ |
| 20 | ❌ `SyntaxError` | ❌ `SyntaxError` |
| 56 | ✅ | ✅ |
| 57 | ❌ `NameError` | ❌ `NameError` |
| 58 | ❌ `NameError` | ❌ `NameError` |
| 59 | ❌ `AssertionError` | ❌ `AssertionError` |
| 61 | ✅ | ✅ |
| 62 | ✅ | ✅ |
| 63 | ❌ `AssertionError` | ❌ `AssertionError` |
| 64 | ✅ | ✅ |
| 65 | ❌ `NameError` | ❌ `NameError` |
| 66 | ✅ | ✅ |
| 67 | ❌ `AssertionError` | ❌ `AssertionError` |
| 68 | ✅ | ✅ |


## MBPP — max_power

Directory: `_benchmark_runs/P3_max_power_mbpp/`

### Run-level summary

| Run file | codegen | pass@1 | passed/total | elapsed (s) |
|---|---|---|---|---|
| `run_pass1.json` | `nanobrain_max_power` | 0.65 | 13/20 | 773.0 |

### Per-problem matrix

| Problem | run_pass1 |
|---|---|
| 11 | ✅ |
| 12 | ✅ |
| 14 | ❌ `AssertionError` |
| 16 | ✅ |
| 17 | ✅ |
| 18 | ✅ |
| 19 | ✅ |
| 20 | ❌ `SyntaxError` |
| 56 | ✅ |
| 57 | ❌ `NameError` |
| 58 | ❌ `NameError` |
| 59 | ❌ `AssertionError` |
| 61 | ✅ |
| 62 | ✅ |
| 63 | ❌ `AssertionError` |
| 64 | ✅ |
| 65 | ✅ |
| 66 | ✅ |
| 67 | ❌ `Timeout` |
| 68 | ✅ |


## MBPP — ablation matrix

Directory: `_benchmark_runs/ablations_mbpp/`

### Run-level summary

| Run file | codegen | pass@1 | passed/total | elapsed (s) |
|---|---|---|---|---|
| `aggregator_memrecorder_r1.json` | `nanobrain_ablation_aggregator_memrecorder` | 0.65 | 13/20 | 195.9 |
| `aggregator_only_r1.json` | `nanobrain_ablation_aggregator_only` | 0.65 | 13/20 | 200.5 |
| `memreader_aggregator_r1.json` | `nanobrain_ablation_memreader_aggregator` | 0.65 | 13/20 | 199.3 |
| `memreader_only_r1.json` | `nanobrain_ablation_memreader_only` | 0.65 | 13/20 | 196.2 |
| `memrecorder_only_r1.json` | `nanobrain_ablation_memrecorder_only` | 0.65 | 13/20 | 195.7 |

### Per-problem matrix

| Problem | aggregator_memrecorder_r1 | aggregator_only_r1 | memreader_aggregator_r1 | memreader_only_r1 | memrecorder_only_r1 |
|---|---|---|---|---|---|
| 11 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 12 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 14 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 16 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 17 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 18 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 19 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 20 | ❌ `AssertionError` | ❌ `AssertionError` | ❌ `AssertionError` | ❌ `AssertionError` | ❌ `AssertionError` |
| 56 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 57 | ❌ `NameError` | ❌ `NameError` | ❌ `NameError` | ❌ `NameError` | ❌ `NameError` |
| 58 | ❌ `NameError` | ❌ `NameError` | ❌ `NameError` | ❌ `NameError` | ❌ `NameError` |
| 59 | ❌ `AssertionError` | ❌ `AssertionError` | ❌ `AssertionError` | ❌ `AssertionError` | ❌ `AssertionError` |
| 61 | ❌ `AssertionError` | ❌ `AssertionError` | ❌ `AssertionError` | ❌ `AssertionError` | ❌ `AssertionError` |
| 62 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 63 | ❌ `AssertionError` | ❌ `AssertionError` | ❌ `AssertionError` | ❌ `AssertionError` | ❌ `AssertionError` |
| 64 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 65 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 66 | ✅ | ✅ | ✅ | ✅ | ✅ |
| 67 | ❌ `Timeout` | ❌ `Timeout` | ❌ `Timeout` | ❌ `Timeout` | ❌ `Timeout` |
| 68 | ✅ | ✅ | ✅ | ✅ | ✅ |


## SciCode val — F17 retrieval_grounded baseline

Directory: `_benchmark_runs/P0_scicode/`

### Run-level summary

| Run file | codegen | pass@1 | passed/total | elapsed (s) |
|---|---|---|---|---|
| `run1.json` | `nanobrain_retrieval_grounded` | 0.00 | 0/5 | 150.0 |
| `run2.json` | `nanobrain_retrieval_grounded` | 0.00 | 0/5 | 146.1 |

### Per-problem matrix

| Problem | run1 | run2 |
|---|---|---|
| 10/10.6 | ❌ `AssertionError` | ❌ `AssertionError` |
| 1/1.1 | ❌ `AssertionError` | ❌ `AssertionError` |
| 3/3.1 | ❌ `AssertionError` | ❌ `AssertionError` |
| 4/4.1 | ❌ `AssertionError` | ❌ `AssertionError` |
| 6/6.1 | ❌ `NameError` | ❌ `NameError` |


## SciCode val — Item 2 perturbed_consensus

Directory: `_benchmark_runs/P1_scicode/`

### Run-level summary

| Run file | codegen | pass@1 | passed/total | elapsed (s) |
|---|---|---|---|---|
| `run1.json` | `nanobrain_perturbed_consensus` | 0.00 | 0/5 | 563.5 |

### Per-problem matrix

| Problem | run1 |
|---|---|
| 10/10.6 | ❌ `AssertionError` |
| 1/1.1 | ❌ `AssertionError` |
| 3/3.1 | ❌ `AssertionError` |
| 4/4.1 | ❌ `AssertionError` |
| 6/6.1 | ❌ `AssertionError` |


## SciCode val — Item 3 integrated_similarity

Directory: `_benchmark_runs/P3_similarity_scicode/`

### Run-level summary

| Run file | codegen | pass@1 | passed/total | elapsed (s) |
|---|---|---|---|---|
| `run_pass1.json` | `nanobrain_integrated_similarity` | 0.20 | 1/5 | 197.3 |
| `run_pass2.json` | `nanobrain_integrated_similarity` | 0.00 | 0/5 | 166.0 |

### Per-problem matrix

| Problem | run_pass1 | run_pass2 |
|---|---|---|
| 10/10.6 | ❌ `AssertionError` | ❌ `AssertionError` |
| 1/1.1 | ❌ `Timeout` | ❌ `AssertionError` |
| 3/3.1 | ❌ `AssertionError` | ❌ `AssertionError` |
| 4/4.1 | ✅ | ❌ `AssertionError` |
| 6/6.1 | ❌ `AssertionError` | ❌ `AssertionError` |


## SciCode val — max_power

Directory: `_benchmark_runs/P3_max_power_scicode/`

### Run-level summary

| Run file | codegen | pass@1 | passed/total | elapsed (s) |
|---|---|---|---|---|
| `run_pass1.json` | `nanobrain_max_power` | 0.20 | 1/5 | 588.5 |

### Per-problem matrix

| Problem | run_pass1 |
|---|---|
| 10/10.6 | ❌ `AssertionError` |
| 1/1.1 | ❌ `Timeout` |
| 3/3.1 | ❌ `AssertionError` |
| 4/4.1 | ✅ |
| 6/6.1 | ❌ `AssertionError` |


## Open-Rosalind v0 — codegen-adapted sequence_basic subset (n=8)

Directory: `_benchmark_runs/open_rosalind_v0/`

### Run-level summary

| Run file | codegen | pass@1 | passed/total | elapsed (s) |
|---|---|---|---|---|
| `direct.json` | `direct` | 0.50 | 4/8 | 47.0 |
| `integrated_similarity.json` | `nanobrain_integrated_similarity` | 0.00 | 0/8 | 135.4 |
| `max_power.json` | `nanobrain_max_power` | 0.00 | 0/8 | 435.5 |
| `perturbed_consensus.json` | `nanobrain_perturbed_consensus` | 0.38 | 3/8 | 378.7 |
| `plan_then_code.json` | `plan_then_code` | 0.00 | 0/8 | 138.5 |
| `retrieval_grounded.json` | `nanobrain_retrieval_grounded` | 0.50 | 4/8 | 107.2 |

### Per-problem matrix

| Problem | direct | integrated_similarity | max_power | perturbed_consensus | plan_then_code | retrieval_grounded |
|---|---|---|---|---|---|---|
| v0/seq-01 | ❌ `ValueError` | ❌ `ValueError` | ❌ `ValueError` | ❌ `ValueError` | ❌ `FileNotFoundError` | ❌ `ValueError` |
| v0/seq-02 | ✅ | ❌ `ValueError` | ❌ `ValueError` | ✅ | ❌ `AssertionError` | ✅ |
| v0/seq-03 | ❌ `ValueError` | ❌ `ValueError` | ❌ `ValueError` | ✅ | ❌ `AssertionError` | ✅ |
| v0/seq-04 | ❌ `ValueError` | ❌ `ValueError` | ❌ `ValueError` | ❌ `ValueError` | ❌ `EOFError` | ❌ `ValueError` |
| v0/seq-05 | ✅ | ❌ `ValueError` | ❌ `ValueError` | ❌ `ValueError` | ❌ `AssertionError` | ✅ |
| v0/seq-06 | ❌ `AssertionError` | ❌ `IndexError` | ❌ `IndexError` | ❌ `ValueError` | ❌ `FileNotFoundError` | ❌ `NameError` |
| v0/seq-07 | ✅ | ❌ `ValueError` | ❌ `ValueError` | ❌ `ValueError` | ❌ `NameError` | ❌ `NameError` |
| v0/seq-08 | ✅ | ❌ `TypeError` | ❌ `TypeError` | ✅ | ❌ `IndexError` | ✅ |


---

## Notes on the data

* Per-problem cells show `✅` for passed, `❌ \`<error_class>\`` for failed. `—` means the problem wasn't in that run's dataset (different dataset slice).
* `error_class` is the test runner's classification: `NonZeroExit` (generated code raised an exception in the test harness), `AssertionError` (test_suite predicate failed), `TimeoutError` (per-problem wall-clock cap exceeded), `SyntaxError` (code didn't parse). When the cell shows just `FAIL`, the harness recorded a failure without categorizing it.
* Wall-time variance within a single (dataset, codegen) cell across runs is bounded by ollama batching variance + system load. The pass@1 and per-problem pass/fail are determined by the LLM output, which is deterministic at T=0 *for a fixed workflow shape*.
* The `integrated_similarity` codegen on SciCode val is the only cell with non-zero spread across passes (0/5 vs 1/5 = 20pp swing). See [`ablation_attribution_memo.md`](./ablation_attribution_memo.md) §"Why SciCode val is non-deterministic" for the mechanism.
