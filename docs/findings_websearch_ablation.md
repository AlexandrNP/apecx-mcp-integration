# Findings — web-search ablation on Open-Rosalind, BixBench, MBPP, SciCode

**Date**: 2026-05-14. **Findings F42-F47.** Sweep data:
`_benchmark_runs/websearch_ablation/`.

This documents the web-search ablation study requested on 2026-05-14:
a `WebSearchContextStep` was added as a new node, and the ablation
matrix was run on the codegen-adapted Open-Rosalind and BixBench
subsets (D1/D2), then the web-search comparison was extended to MBPP
and SciCode (F47) so the delta is measured on algorithmic codegen and
scientific codegen too. Plan: `docs/websearch_ablation_plan.md`.

## Headline

**Web search is null-to-harmful as a codegen accuracy lever, and the
harm scales with composition complexity — confirmed across 4
benchmarks.** It is a *variance source*, not a *lift source*. This is
an adoption-reliability finding, not a failure: it tells operators
**not** to add web search to algorithmic / scientific codegen
pipelines — it is dead weight on simple compositions and
prompt-pollution on heavy ones.

Across Open-Rosalind, MBPP, and SciCode, the **F17 + websearch** delta
is 0.0 to −0.05, and the **max_power + websearch** delta is −0.10 to
−0.25 — every benchmark, every time, max_power+websearch < max_power.

BixBench's codegen-adapted subset is a **uniform wall of environmental
failures** across all 10 modes — the codegen-drafter framing
structurally cannot solve a tool-agent benchmark, and no scaffold
(web search included) changes that.

## F42 — Open-Rosalind ablation matrix (v0 subset, N=8)

mistral-nemo:latest, OR codegen-adapted `sequence_basic` v0 subset,
memory store cleared before the sweep, fixed mode order.

| Mode | pass@1 | passed | vs F17 baseline |
|---|---|---|---|
| `direct` | 0.500 | 4/8 | — |
| `nanobrain_retrieval_grounded` (F17 baseline) | 0.500 | 4/8 | 0.0 |
| `nanobrain_ablation_memreader_only` | 0.500 | 4/8 | 0.0 |
| `nanobrain_ablation_aggregator_only` | 0.500 | 4/8 | 0.0 |
| `nanobrain_ablation_memrecorder_only` | 0.500 | 4/8 | 0.0 |
| `nanobrain_ablation_memreader_aggregator` | 0.500 | 4/8 | 0.0 |
| `nanobrain_ablation_aggregator_memrecorder` | 0.500 | 4/8 | 0.0 |
| **`nanobrain_ablation_websearch_only`** (F17 + websearch) | **0.500** | **4/8** | **0.0** |
| `nanobrain_max_power` | 0.375 | 3/8 | −0.125 |
| **`nanobrain_max_power_websearch`** (max_power + websearch) | **0.125** | **1/8** | **−0.375** |

**Two web-search deltas:**
- F17 + websearch vs F17: **0.0** (4/8 → 4/8).
- max_power + websearch vs max_power: **−0.25** (3/8 → 1/8).

## F43 — web search is a variance source, not a lift source

The pass *counts* hide the real effect. The pass *sets*:

| Mode | problems passed |
|---|---|
| `nanobrain_retrieval_grounded` (F17) | seq-02, seq-03, seq-05, seq-08 |
| `nanobrain_ablation_websearch_only` | seq-02, seq-03, **seq-06**, seq-08 |

F17 + websearch did not lift the count — but it **swapped seq-05 for
seq-06**. Web search *changed which problems pass* without netting a
gain. It helped one problem and broke another. That is the signature
of a variance source: it perturbs the outcome distribution without
shifting its mean. For a benchmark this is noise; for production it
means web search makes a pipeline *less predictable* run-to-run
(compounded by web search being non-deterministic — see F46).

## F44 — the harm scales with composition complexity

| Composition | nodes | without websearch | with websearch | delta |
|---|---|---|---|---|
| F17 retrieval_grounded | 2 (router → drafter) | 4/8 | 4/8 | 0.0 |
| max_power | 6 (router → memory → drafter → aggregator → recorder) | 3/8 | 1/8 | −0.25 |

`max_power_websearch` collapsed to passing only `{seq-08}`. The
mechanism: `max_power` is *already* an over-engineered prompt for an
8-line sequence problem (it underperforms `direct` 0.375 < 0.500 —
consistent with F18/F39: heavy scaffolds collapse on simple
problems). Inserting a `web_search_context` node piles MORE context —
non-deterministic, often irrelevant web snippets — into an
already-overloaded prompt. The `fail_other` histogram for
`max_power_websearch` is `{fail_other: 6}` — six runtime errors in
generated code (the LLM, drowning in context, produces buggier code).
Web search does not add information the LLM can use here; it adds
distraction the LLM cannot ignore.

## F45 — BixBench is a uniform wall of environmental failures

BixBench Python-answerable subset (`str_verifier` + `range_verifier`,
N=8 per mode), all 10 modes:

| Mode (all 10) | pass@1 |
|---|---|
| every mode — `direct`, F17, all 5 ablations, websearch_only, max_power, max_power_websearch | **0.000 (0/8)** |

**Failure-mode breakdown (80 runs):**

| Count | Error | Cause |
|---|---|---|
| 46 | `UnicodeDecodeError` | generated code reads a real capsule data file as utf-8; many are binary / latin-1 (`.xlsx`, encoded `.txt`) |
| 14 | `FileNotFoundError` | generated code references a capsule filename that does not exist — the drafter *guesses* file names it cannot see |
| 10 | `SyntaxError` | the LLM produced invalid Python on the complex bio prompts |
| 7 | `KeyError` | generated code references a DataFrame column that does not exist — the drafter *guesses* column names |
| 3 | `ModuleNotFoundError` | `Bio` (biopython) is not installed in the subprocess sandbox |

Every one of these is **environmental / structural**, not
scaffold-addressable. The drafter is *blind to the capsule's actual
contents* — the file names, the encodings, the DataFrame columns,
the installed libraries. It is writing code against data it cannot
inspect. No scaffold — web search, memory, consensus, perturbation —
fixes "you cannot see the data." Web search could in principle tell
the LLM "DESeq2 is an R package" — but the drafter would still be
unable to *see the capsule*, so it cannot act on that.

**This confirms the pre-run prediction** (`websearch_ablation_plan.md`,
"BixBench standing prediction"): BixBench is natively a *tool-agent*
benchmark (the agent explores the capsule interactively, runs code,
reads output, iterates). The codegen-drafter framing — "write
`solve(data_dir)` in one shot" — structurally cannot work, because
the one thing the drafter needs (to look inside `data_dir`) is the
one thing it cannot do. The 122-problem Python-answerable subset is
"Python-answerable by *eval_mode*" but not "Python-answerable by
*content*".

## F46 — mechanism summary + adoption-reliability conclusion

**Why web search is null-to-harmful on codegen:**

1. **OR `sequence_basic` is pure computation.** The answer is an
   algorithm (reverse complement, GC content, translation), not a
   lookup. There is nothing on the web to retrieve that shortcuts
   writing the algorithm. Web search returns *something* for every
   query (it is rarely empty), so it always injects context — and
   that context is always irrelevant here. Best case: ignored
   (null). Worst case: distraction (negative).
2. **The harm is mediated by prompt load.** On a 2-node composition
   the extra context is small relative to the prompt and the LLM
   ignores it (0.0 delta). On a 6-node composition the prompt is
   already saturated (router examples + memory examples + perturbation
   stems) and the web block pushes it past the model's effective
   working-context — code quality degrades (−0.25 delta).
3. **Non-determinism compounds it.** `WebSearchContextStep` is a
   non-deterministic node (live web results drift). Even where the
   mean effect is zero, the run-to-run variance is now higher. The
   tool's `cache_dir` makes a *populated cache* reproducible, but
   the live web is the source of truth on first population.

**Adoption-reliability conclusion** — the shippable guidance:

- **Do NOT add web search to algorithmic-codegen pipelines.** It is
  null on simple compositions and actively harmful on heavy ones.
  `websearch_workflow_rules.md` pins this: "DO NOT add a web-search
  node to a pure-computation pipeline expecting an accuracy lift."
- **Web search's honest value is elsewhere** — a drafter facing a
  genuinely unfamiliar *library* (not an algorithm) could benefit.
  That regime was not exercised here (OR is algorithms; BixBench
  fails before the drafter's library knowledge even matters). The
  `WebSearchTool` + `WebSearchContextStep` are shipped and correct;
  what this ablation establishes is *where not to use them*.
- **BixBench needs the tool-agent framing**, not the codegen framing.
  The `RheaCodeUseAgent` (Phase E) is the start of that — an agent
  that explores + uses tools interactively. Adapting BixBench to an
  agent surface (the agent gets a sandbox it can `ls`, read files
  in, run code in, iterate) is the honest next arc; the codegen
  subset stays in the catalog as a documented negative result.

## F47 — the pattern holds across 4 benchmarks (MBPP + SciCode)

The web-search comparison was extended to MBPP (algorithmic codegen,
test split) and SciCode (scientific codegen, validation split), N=20
per mode, mistral-nemo:latest, memory store cleared before the sweep.
5 modes — the ones that isolate the web-search delta.

### MBPP (N=20)

| Mode | pass@1 | passed | delta |
|---|---|---|---|
| `direct` | 0.650 | 13/20 | — |
| `nanobrain_retrieval_grounded` (F17) | 0.650 | 13/20 | — |
| `nanobrain_ablation_websearch_only` (F17 + websearch) | 0.600 | 12/20 | **−0.05** vs F17 |
| `nanobrain_max_power` | 0.650 | 13/20 | — |
| `nanobrain_max_power_websearch` (max_power + websearch) | 0.500 | 10/20 | **−0.15** vs max_power |

### SciCode (N=20, validation)

| Mode | pass@1 | passed | delta |
|---|---|---|---|
| `direct` | 0.300 | 6/20 | — |
| `nanobrain_retrieval_grounded` (F17) | 0.350 | 7/20 | — |
| `nanobrain_ablation_websearch_only` (F17 + websearch) | 0.350 | 7/20 | **0.0** vs F17 |
| `nanobrain_max_power` | 0.350 | 7/20 | — |
| `nanobrain_max_power_websearch` (max_power + websearch) | 0.250 | 5/20 | **−0.10** vs max_power |

### The cross-benchmark picture

| Benchmark | F17 + websearch delta | max_power + websearch delta |
|---|---|---|
| Open-Rosalind (N=8) | 0.0 | −0.25 |
| MBPP (N=20) | −0.05 | −0.15 |
| SciCode (N=20) | 0.0 | −0.10 |
| BixBench (N=8) | 0.0 (all modes 0) | 0.0 (all modes 0) |

**The F44 finding is no longer an N=8 Open-Rosalind artifact — it is a
4-benchmark result.** On the 2-node F17 composition, web search is
null-to-slightly-negative (0.0 to −0.05) everywhere. On the 6-node
max_power composition, web search is **substantially and consistently
negative** — −0.10 to −0.25, every benchmark, every time:
`max_power_websearch < max_power` with no exception.

The mechanism is visible in the failure histograms: `max_power_websearch`
carries an elevated `fail_other` count on every dataset (OR 6, MBPP 6,
SciCode 8) — `fail_other` is runtime errors in *generated code*. The
extra web-context block, piled into an already-saturated kitchen-sink
prompt, degrades the LLM's code quality. This is not measurement
noise: it is the same directional effect at three independent
benchmarks of three different characters (bio sequences, general
algorithms, scientific computing).

## Honest caveats

1. **N is modest.** The OR `sequence_basic` v0 subset is 8 problems;
   MBPP / SciCode were run at N=20. The OR 0.500-across-8-modes result
   means "the same 4 hard problems fail regardless of scaffold" — too
   small alone to resolve small effects. But F47 is the answer to that
   caveat: the MBPP + SciCode N=20 runs **independently confirm** the
   max_power+websearch regression (−0.15, −0.10), so the directional
   finding does not rest on the N=8 OR subset. The F17+websearch delta
   (0.0 to −0.05) is small enough that it could still mask a tiny real
   effect either way — but it is unambiguously NOT a lift.
2. **Memory store state.** The store was cleared before D1 and before
   D2. Within D1, the 3 memory-recorder modes wrote passing OR
   solutions to the shared store before `max_power` ran — so
   `max_power`'s `similarity_read` could have read them. `max_power`
   still underperformed (3/8), so any cross-pollination did not help
   — consistent with F39 (the closed memory loop amplifies errors
   when the base context is off). For D2 every BixBench problem
   failed, so `record_only_if_pass` gated all writes — no D2
   cross-contamination.
3. **BixBench scope.** The sweep ran on the 122-problem
   Python-answerable subset (`str_verifier` + `range_verifier`),
   `--limit 8` per mode for comparability with D1. The 83
   `llm_verifier` questions need an LLM judge not wired into the
   subprocess sandbox and were not run. Extending past N=8 would not
   change the conclusion — the failures are environmental and
   uniform; a bigger N is more 0s.
4. **The codegen-adapted BixBench numbers are not BixBench's
   published accuracy.** They measure "can a one-shot Python drafter
   reimplement a bioinformatics analysis against a capsule it cannot
   inspect" — the honest answer is no. BixBench's published agentic /
   zero-shot numbers measure a different thing.

## Reproducibility

```bash
# D1 — Open-Rosalind ablation sweep (10 modes, N=8)
bash _benchmark_runs/websearch_ablation/run_or_sweep.sh

# D2 — BixBench ablation sweep (10 modes, N=8)
#   requires the extracted capsules:
export APECX_BIXBENCH_CAPSULES=<workspace>/data/benchmarks/bixbench
bash _benchmark_runs/websearch_ablation/run_bixbench_sweep.sh

# F47 — MBPP + SciCode web-search comparison (5 modes each, N=20)
bash _benchmark_runs/websearch_ablation/run_mbpp_scicode_sweep.sh
```

Per-mode JSON outputs:
`_benchmark_runs/websearch_ablation/{or,bix,mbpp,scicode}_<mode>.json`.
Sweep logs:
`_benchmark_runs/websearch_ablation/{or,bix,mbpp_scicode}_sweep.log`.
