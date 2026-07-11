# Multi-judge relevance assessment — full run instructions (for /schedule or a local scheduler)

This runbook drives the multi-model, multi-judge relevance study to completion: judge up to **4000**
distinct served records with a **panel of 6 local LLM models** + the 3 automated judges, then compute
**per-judge precision / recall / F1 / confusion per request type** and an **inter-judge Cohen-κ matrix**.
It is **resumable** — safe to kill and re-launch (each model's labels are checkpointed).

## ⚠️ Execution constraint — this runs LOCALLY, not in the cloud
The 6 judges are **local Ollama** models (`localhost:11434`). A **cloud `/schedule` cannot reach them**,
so this job MUST run on the machine where Ollama + the data live. Use the local recipes below. (A cloud
schedule is only viable if `APECX_LLM_BASE_URL` points at a cloud-reachable endpoint hosting these
models — not the case here.)

## Expected cost
On this hardware a 24B judgment is ~5s and Ollama does not parallelize, so the full pass is
**~15–20 GPU-hours** (roughly: nemotron-4B ~1.7h, medgemma/medllama2/gemma-8B ~2h each, mistral-nemo-12B
~3h, devstral-24B ~6h). Models run **one-fully-before-the-next** (fast→slow) to avoid Ollama reloads;
partial results favour the cheap models first. It saturates the GPU for the duration.

## Prerequisites (verify once)
```bash
cd /Users/onarykov/Downloads/apecx-cowork/wt-harmonization-eval
# 1. The 6 panel models are present:
curl -s localhost:11434/api/tags | python3 -c "import sys,json;print(sorted(m['name'] for m in json.load(sys.stdin)['models']))"
#    expect: devstral:24b, gemma4:8b, medgemma:latest, medllama2:7b, mistral-nemo:12b, nemotron-3-nano:4b
# 2. The core results JSON exists (it holds the served-record sample rows + each record's automated
#    Judge A / Judge B / combined verdict — the panel judges these, no Globus re-fetch):
ls -la tests/eval/harmonization/output/harmonization_precision.json
# 3. The dictionary path (only needed if regenerating the core JSON; NOT needed for this judge pass):
#    APECX_SYNONYM_DICT_PATH=~/.apecx/dictionary/dictionary.sqlite
```

## The ONE launch command (background, resumable)
```bash
cd /Users/onarykov/Downloads/apecx-cowork/wt-harmonization-eval
PYTHONPATH=src:. nohup .venv/bin/python -m tests.eval.harmonization.run_judge_validation \
  --core tests/eval/harmonization/output/harmonization_precision.json \
  --n 4000 --workers 4 \
  > tests/eval/harmonization/output/multijudge.log 2>&1 &
echo "launched PID $!"
```
- `--n 4000` distinct records per model · `--workers 4` (Ollama serialises 24B; 4 avoids timeout thrash).
- `--panel "<tag>,<tag>,..."` overrides the default 6-model panel.
- Writes per-model checkpoints `output/judge_labels.<model>.jsonl` and, at the end, `per_judge_stats`
  + `inter_judge_kappa` into the core JSON.

## Monitor
```bash
tail -f tests/eval/harmonization/output/multijudge.log         # per-model "[Ns] model: k/N" progress
wc -l tests/eval/harmonization/output/judge_labels.*.jsonl     # labels done per model
```

## Resume (after a kill / reschedule)
Re-run the **exact same launch command**. Each model skips records already in its
`judge_labels.<model>.jsonl` and continues; a model whose checkpoint is complete is skipped instantly.
Nothing is lost and nothing is double-judged.

## Done / acceptance
The run prints `DONE. n_records=<~4000>` and a per-judge precision/recall table, and the core JSON gains
`per_judge_stats` (`by_category_majority`, `by_regime_majority`, `by_category_combined`) +
`inter_judge_kappa`. Every judge must carry non-null **precision AND recall** in the by-category block.
Then regenerate the findings "Multi-judge panel" section from the JSON.

## Scheduling recipes

### A. Session `/loop` (simplest — this session or any open terminal)
```
/loop 20m Check tests/eval/harmonization/output/multijudge.log; if the run died, re-launch the runbook
command (it resumes); when it prints DONE, fill the findings Multi-judge section from per_judge_stats and stop.
```
The `/loop` re-launches on failure (resume is free) and finalizes on completion. Needs the terminal open.

### B. Local `launchd` (survives terminal close; macOS-native)
Create `~/Library/LaunchAgents/com.apecx.multijudge.plist` running the launch command once at load
(`RunAtLoad=true`, `KeepAlive=false`); `launchctl load` it. Because the runner is resumable, a
`StartInterval` (e.g. 3600s) that re-invokes it also works — each tick resumes and a completed run is a
no-op. Unload when `n_records≈4000` is reached.

### C. Local `cron`
```
*/30 * * * * cd /Users/onarykov/Downloads/apecx-cowork/wt-harmonization-eval && PYTHONPATH=src:. .venv/bin/python -m tests.eval.harmonization.run_judge_validation --n 4000 --workers 4 >> tests/eval/harmonization/output/multijudge.log 2>&1
```
Same idempotent-resume property; remove the crontab line once the run completes.

## Notes / honesty
- **meditron:7b was tried and dropped** — it echoes the system prompt via chat (0 parseable verdicts)
  and ignores instructions via completion. `medgemma` (Gemma-based) replaced it and follows the JSON
  contract like `gemma4`. `medllama2` answers in PROSE, rescued by `llm_validate._prose_belongs`.
- The two bio models are medical/clinical finetunes, **not virology-specific** — their abstain-rate and
  agreement are reported so their (limited) domain fit is visible, not assumed.
- precision/recall are **agreement vs a proxy-gold** (panel-majority, leave-one-out; and the combined-
  automated anchor) — there is no absolute ground truth; read with the κ caveat.
