# Biology benchmark extension — request + blockers + plan

**Date**: 2026-05-14.
**Request**: extend the cgu-codegen-uplift assessment matrix with biology
benchmarks: BixBench, Open-Rosalind, BioDesignBench, BioML-bench,
BioProBench.

## STATUS UPDATE (2026-05-14, post-integration)

The user provided source links and the instruction "first configure +
integrate ALL benchmarks (canonical splits), then run." That was done.
Current status:

| Benchmark | Source | Loader | Status |
|---|---|---|---|
| **Open-Rosalind** | github.com/maris205/open-rosalind | `open_rosalind.py` | **CONFIGURED + RAN** — 6 codegens × n=8 codegen-adapted `sequence_basic` subset, splits v0/v1/holdout |
| **BixBench** | github.com/Future-House/BixBench + HF | `bixbench.py` | **CONFIGURED, run blocked** — 5.91 GB capsules + R tooling + LLM judge |
| **BioML-bench** | github.com/science-machine/biomlbench | `biomlbench.py` | **CONFIGURED, runs deferred** per user instruction |
| **BioProBench** | huggingface.co/BioProBench | `bioprobench.py` | **CONFIGURED, runs deferred** per user instruction |
| **BioDesignBench** | — | — | **DROPPED** — no source link provided in the user's link set |

All 4 loaders are framework-agnostic (`BenchmarkProblem` iterator
pattern), CLI-wired, smoke-tested (8 tests in
`tests/benchmarks/test_biology_loaders.py`), and FAIL LOUDLY when
invoked without prerequisites — none skip-silently.

Results + mechanism analysis: see
[`findings_biology_benchmarks.md`](./findings_biology_benchmarks.md)
(findings F36-F41). The Open-Rosalind sweep produced a significant
adoption-reliability finding (F39): the closed memory loop amplifies
errors when the domain's base accuracy is low.

The sections below are the ORIGINAL pre-integration pushback +
planning doc, preserved as the reasoning trail.

---

## Brutal-truth pushback (the original why-no-code section)

The user mandate is "no silent failures that would make tests pass but
impede actual product use." Per CLAUDE.md the workspace rule is **real
data, not synthetic** — mocks are acceptable only for smoke tests; a
component is never considered complete or fully tested without an
integration test against real data.

For these 5 benchmarks:

1. **ZERO datasets are present in the workspace** (verified by recursive
   `find` across `/Users/onarykov` depth 4 — no matches for any of the 5
   benchmark names).
2. **ZERO of the benchmarks have existing loaders, scoring harnesses, or
   tooling** in this repo. Only `mbpp`, `scicode`, and `nanobrain_native`
   are wired.
3. **Several of these benchmarks require specialized scientific tooling**
   (BioPython for sequences, RDKit for chemistry, AlphaFold or
   ESMFold for structure prediction, sklearn / xgboost for ML scoring)
   — none of which are in the project's pinned dependency set.
4. **Pure-LLM-prompt benchmarks won't work for the bio-design and bio-
   prediction benchmarks.** The scoring rubrics are domain-specific
   (RMSD for structures, AUC for ML models, ΔΔG for binding affinity)
   and require running the candidate code AGAINST scientific oracles
   (PDB structures, gene-expression datasets, binding-affinity tables).

**If I scaffolded loaders without real data and ran sweeps**, the
resulting numbers would be **fake**. The harness would not actually
verify biological correctness — it would just verify the candidate code
runs without exception. That's precisely the silent-failure shape the
workspace policy forbids.

**Honest verdict**: I will NOT ship "BixBench results" or any of the
other 4 against `mistral-nemo:latest` until each benchmark's real data
+ canonical scoring harness is in the workspace.

This document is the planning artifact. Code lands after data does.

## What each benchmark is (training-knowledge with caveats)

I am NOT going to claim verified knowledge of exactly which version,
data format, or scoring rubric is canonical for each — that's the user's
call. The descriptions below are best-effort summaries based on training
knowledge and may be wrong or outdated. The user should confirm the
canonical source for each.

### BixBench (Bioinformatics task benchmark)

- **What**: end-to-end bioinformatics task automation. Tests LLM ability
  to write code that performs gene expression analysis, variant calling,
  read alignment, etc. Multi-step problems with intermediate verification.
- **Likely source**: GitHub `FutureHouseSF/BixBench` (2024 release).
  Has ~50 problems each with a setup notebook + expected output.
- **Scoring**: per-problem pass/fail on numeric output match (e.g.,
  did the candidate's variant-calling pipeline produce the same VCF
  as the reference?).
- **Tooling required**: bcftools, samtools, scanpy, possibly a local
  reference genome (~3 GB).
- **Honest assessment**: HIGH integration cost. Each problem requires
  its own setup environment. May need Docker isolation for the bio-
  tooling. This is a multi-day integration, not a multi-hour one.

### Open-Rosalind BioBench (Rosalind problem set adaptation)

- **What**: bioinformatics algorithm problems from the
  [rosalind.info](https://rosalind.info) problem tree. Each problem is
  a short text description + input stream + expected output stream.
- **Likely source**: GitHub `<various forks>/open-rosalind` —
  several community ports exist. None is "the canonical open version"
  as far as I'm aware.
- **Scoring**: exact text output match (sequence motif, count, etc.).
  No specialized tooling needed beyond standard Python.
- **Tooling required**: none beyond Python stdlib + BioPython
  (already in many test environments).
- **Honest assessment**: LOWEST integration cost of the 5. Each
  problem is a single function with stdin → stdout. Could be
  integrated in a few hours IF the user points at the specific
  open-rosalind fork they want.

### BioDesignBench (biological design tasks)

- **What**: probably synthetic-biology / protein design tasks (design
  a sequence with property X, design a regulatory circuit). Possibly
  multi-modal (sequence + structure).
- **Likely source**: I do NOT have high-confidence knowledge of this
  benchmark by exactly this name. There are benchmarks like
  `ProteinGym` and `MoleculeNet` that fit the spirit but I'm not
  asserting either IS BioDesignBench.
- **Scoring**: domain-specific (functional assay scores, structure
  RMSD, binding-affinity predictions).
- **Tooling required**: heavy. Likely needs ESMFold or AlphaFold for
  structure scoring; possibly molecular docking software for ligand
  problems.
- **Honest assessment**: HIGHEST integration cost. The scoring oracle
  may require GPU + multi-GB models. Not a multi-hour task.

### BioML-bench (ML on biological data)

- **What**: ML benchmark suite for biology. Tests LLM ability to write
  code that builds ML models on biological datasets (gene expression
  prediction, drug-target binding, etc.).
- **Likely source**: possibly the `bioml-bench` package on PyPI; possibly
  the `TDC` (Therapeutic Data Commons) benchmark suite.
- **Scoring**: AUC, AUPRC, MSE, etc. depending on the task. Each
  problem has held-out test data.
- **Tooling required**: sklearn, torch (optional), the specific data
  splits.
- **Honest assessment**: MEDIUM integration cost. The scoring is
  standard ML metrics; data splits are typically distributed as
  HuggingFace datasets or `tdc` package.

### BioProBench (protein benchmark)

- **What**: protein-related tasks. Possibly:
  (a) protein-structure prediction (input sequence, output structure),
  (b) protein-property prediction (input structure, output property),
  (c) protein-design (specify property, output sequence).
- **Likely source**: I am NOT confident this exists by exactly this
  name. There's `ProteinGym`, `CASP`, `PEER`, `TAPE` — multiple
  protein benchmarks under various names.
- **Scoring**: highly task-specific.
- **Tooling required**: heavy. Likely AlphaFold + structure-comparison
  tooling.
- **Honest assessment**: HIGHEST integration cost (tied with
  BioDesignBench). Same GPU + multi-GB-model caveats apply.

## What I would scaffold IF the user confirms each benchmark

For each benchmark, the framework-native integration would be:

1. **`tests/benchmarks/datasets/<name>.py`** — a loader following the
   pattern of `mbpp.py` / `scicode.py` / `nanobrain_native.py`. Returns
   an iterator of `BenchmarkProblem` dataclasses with
   `(problem_id, prompt, setup_code, test_code, entry_point,
   test_hint, function_signature)`.

2. **`tests/benchmarks/cli.py` integration** — add the new dataset to
   the `dataset` positional argument's choices, plus any benchmark-
   specific flags (e.g., `--bixbench-data-root`, `--rosalind-fork`).

3. **`tests/benchmarks/sandbox.py` adaptation** — if the benchmark
   requires bio-tooling, the subprocess sandbox needs to PATH-include
   the relevant binaries. Per workspace policy (no synthetic data),
   the user must ensure those binaries are installed.

4. **Per-benchmark dependency declaration** — `pyproject.toml` extras
   per benchmark (`bixbench` extra, `bioml` extra, etc.) so adopters
   can `pip install -e '.[bixbench]'` cleanly.

5. **Documentation** — a per-benchmark `docs/<name>_setup.md` with
   download links, env setup, troubleshooting.

6. **Smoke test** — `tests/integration/test_<name>_loader.py` that
   exercises the loader on a small subset of real data, confirms
   problems parse, confirms test_code is executable. Skipped when
   data isn't installed.

**Total estimated effort per benchmark** (after data is available):
- BixBench: 6-10 hours (bio-tooling integration is non-trivial).
- Open-Rosalind: 2-4 hours (cheapest, pure Python).
- BioDesignBench: 1-3 days (heavy tooling).
- BioML-bench: 4-8 hours (standard ML harness).
- BioProBench: 1-3 days (heavy tooling).

**Grand total**: ~3-7 days of focused integration work. NOT a single
multi-hour session.

## Concrete user actions needed to unblock

Before ANY of these 5 benchmarks can produce real numbers, the user
must specify:

1. **For each benchmark, the canonical source** (URL or path):
   - BixBench: which GitHub repo? Which release tag?
   - Open-Rosalind: which fork? Which problem subset?
   - BioDesignBench: confirm this is the canonical name; provide source.
   - BioML-bench: PyPI package? GitHub repo? TDC subset?
   - BioProBench: confirm canonical name; provide source.

2. **Data installation pathway** — for each benchmark:
   - Where should the data live in the workspace? Suggested layout:
     `data/benchmarks/<benchmark_name>/`
   - Are there licensing constraints on bundling the data with the
     repo, or must it be downloaded per-deployment?

3. **Tooling installation strategy** — for benchmarks requiring bio-
   tooling (BixBench, BioDesignBench, BioProBench):
   - Conda env? Docker container? System packages?
   - Are GPU resources available for structure prediction?

4. **Scoring rubric confirmation** — for each benchmark, what counts
   as a "pass"? Exact match? Within-tolerance? Distribution-level?

5. **Prioritization** — if all 5 benchmarks would take ~3-7 days, which
   subset should ship first? My honest recommendation:
   - **Open-Rosalind** first (cheapest, fastest to integrate, lowest
     dependency surface).
   - **BioML-bench** second (medium cost, demonstrates ML-on-biology
     surface).
   - **BixBench** third (high cost but high adoption-pitch value).
   - **BioDesignBench and BioProBench** last (heaviest tooling).

## What I will NOT do without explicit user direction

- Fabricate any of the 5 benchmark loaders with placeholder data and
  run sweeps that produce fake numbers. This is exactly the
  silent-failure mode the workspace policy is built to prevent.
- Stub loaders that pretend to work but actually skip because data is
  missing. Skipped-but-counted-as-tested is worse than absent.
- Run any of the existing 4 codegens (F17, perturbed, integrated_similarity,
  max_power) against fabricated bio data and claim it generalizes to
  these benchmarks.
- Pull random GitHub repos named "bixbench" or "biodesign" without
  the user confirming which is canonical. Multiple unrelated projects
  can share names.

## What the user can reasonably ask me to do next

- **(A)** confirm one benchmark at a time (e.g., "BixBench is at
  github.com/FutureHouseSF/BixBench, here's the install command, here's
  the data path") and I scaffold the loader + CLI integration + smoke
  test for that ONE benchmark. Then we measure that ONE benchmark.

- **(B)** treat the existing 3-benchmark matrix (nanobrain-native +
  MBPP + SciCode val) as the complete assessment and ship the codegen
  uplift work as-is. The biology benchmarks become a separate work
  arc with its own task tree.

- **(C)** explicit scope override: "ignore the no-synthetic-data rule,
  fabricate placeholder data, run the sweeps anyway." I would do this
  ONLY with the user's explicit override AND a clear note in the
  finding doc that the numbers are not biologically meaningful. This
  is the path I least recommend.

**My honest recommendation**: option (A) with **Open-Rosalind first**,
because it's the cheapest integration AND its problem set is the closest
algorithmic-adjacent to MBPP. We could ship Open-Rosalind in a single
multi-hour session if the user provides the canonical fork + a small
data subset.

## Adjacent considerations for adoption reliability

If we ship a biology-benchmark surface, **adoption-reliability concerns
multiply**:

1. **Per-user data isolation**: bio data is often sensitive (clinical,
   IP-protected). The closed memory loop from `integrated_similarity`
   would need explicit per-user scoping. Currently the memory store is
   process-global.

2. **Bio-tooling lifecycle**: if a workflow depends on `bcftools v1.17`
   and an adopter has `v1.21`, results may differ. The
   `Workflow.checksum()` proposal from F31 would help, but it doesn't
   capture external-tool versions.

3. **Reproducibility across machines**: deterministic-at-T=0 only holds
   if every machine's GPU, BLAS, BioPython version, etc. match. Bio
   pipelines amplify this variance.

4. **Cost transparency**: a single BioDesignBench problem could
   trigger an AlphaFold run that costs $1-10 in GPU time. The
   workflow's `CostEnvelope` (G26) needs per-step cost annotations
   for these benchmarks. We don't have that yet.

These are real adoption-reliability concerns that the existing
3-benchmark surface doesn't expose. Shipping the bio benchmarks before
addressing them would push silent failures into production.

## Final stance

**This document is the deliverable for this turn.** No code, no fake
sweeps, no fabricated numbers. The biology-benchmark extension is a
real and valuable next-iteration target, but it is a multi-day
integration project gated on user-specified data sources, tooling
installation, and adoption-reliability decisions.

I'm willing to start option (A) with Open-Rosalind as soon as the user
points at the canonical fork OR confirms an alternative bio benchmark
that has a similar pure-Python scoring surface.

**Brutal-truth on the user's request as written**: "include 5 biology
benchmarks in the full assessment" cannot be satisfied in this session
without violating the no-synthetic-data rule. I'm pushing back because
the alternative is producing fake measurements that look authoritative
but are not. The user's standing instruction "no silent failures that
make tests pass but impede actual product use" is the same rule I'm
applying here at the dataset layer.
