#!/bin/bash
# Run a single codegen pattern across MBPP/SciCode/nanobrain_native.
# Used by the G96 benchmark sweep. Sequential because Ollama is single-GPU
# bottlenecked — parallel runs just thrash the queue.
#
# Usage: scripts/run_pattern_sweep.sh <codegen-name>
#
# Example:
#   scripts/run_pattern_sweep.sh hd_rss
#   → /tmp/bench_mbpp_hd_rss_n100.json
#   → /tmp/bench_scicode_hd_rss_full.json
#   → /tmp/bench_nbnative_hd_rss_full.json
#
# All output to /tmp/sweep_<codegen>.log
set -u
codegen="${1:?usage: $0 <codegen-name>}"
log_file="/tmp/sweep_${codegen}.log"
model="${APECX_BENCH_MODEL:-mistral-nemo:latest}"

cd "$(dirname "$0")/.."

echo "=== sweep started at $(date) for codegen=$codegen model=$model ===" > "$log_file"

for spec in "mbpp::100" "scicode::40" "nanobrain_native::10"; do
  dataset="${spec%%::*}"
  limit="${spec##*::}"
  # Normalize the output filename: nanobrain_native → nbnative for shorter file names.
  out_dataset="${dataset/nanobrain_native/nbnative}"
  out_path="/tmp/bench_${out_dataset}_${codegen}_$([ "$limit" = "100" ] && echo "n100" || echo "full").json"
  echo "" >> "$log_file"
  echo "--- $(date) ${dataset}/${codegen} limit=${limit} → ${out_path} ---" >> "$log_file"
  PYTHONPATH=src:. .venv/bin/python -m tests.benchmarks.cli "$dataset" \
    --codegen "$codegen" \
    --model "$model" \
    --limit "$limit" \
    --output "$out_path" >> "$log_file" 2>&1
  rc=$?
  echo "exit=$rc" >> "$log_file"
done

echo "" >> "$log_file"
echo "=== SWEEP DONE at $(date) for codegen=$codegen ===" >> "$log_file"
