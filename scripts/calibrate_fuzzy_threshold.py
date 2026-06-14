"""SC-E5 — fuzzy-threshold calibration over ``synonym_probe_v1.jsonl``.

Replays the probe set at varying fuzzy floors {0.60, 0.65, 0.70, 0.75,
0.80, 0.85} and reports the recall-vs-precision trade-off so the 0.70
default can be confirmed (or revised) with evidence before the
dictionary ships to scientists.

What this measures:

- **Typo recall** — fraction of ``scenario=typo`` probes that lift to
  SOMETHING (fuzzy_resolved OR ambiguous). Higher is better — these are
  real user queries the dictionary is asked to handle.
- **Unresolvable false-positive rate** — fraction of
  ``scenario=unresolvable`` probes that get lifted to anything other
  than ``miss``. Zero is ideal; non-zero means the HITL queue fills
  with garbage and operators learn to ignore it.

What this does NOT measure:

- IRI correctness of each typo's lift. Without per-row ground truth
  (SC-E1's ``expected_iri`` covers ~70% of typos; the rest are
  honestly-unknown). A future SC-E5b can layer that on.
- Behavior at the 0.85 ``FUZZY_RESOLVED`` band gate or the ±0.05
  near-tie window. Those are held constant; this script varies only
  the admission floor.

Usage:

    .venv/bin/python scripts/calibrate_fuzzy_threshold.py
    .venv/bin/python scripts/calibrate_fuzzy_threshold.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

if __name__ == "__main__" and __package__ in (None, ""):
    _SRC_DIR = Path(__file__).resolve().parents[1] / "src"
    if (_SRC_DIR / "apecx_integration" / "__init__.py").exists():
        sys.path.insert(0, str(_SRC_DIR))

from apecx_integration.synonym_dictionary.enums import EntityType  # noqa: E402
from apecx_integration.synonym_dictionary.loader import (  # noqa: E402
    configure_dictionary_path,
    get_dictionary_index,
)

# Match the production gate constants pinned in lookup_entity.
_FUZZY_RESOLVED_FLOOR = 0.85
_NEAR_TIE_MARGIN = 0.05

# The probe categories that are immune to fuzzy threshold changes
# (they short-circuit on the fast path before fuzzy fires).
_FAST_PATH_SCENARIOS = frozenset({"scientific_name", "acronym", "common_name"})

# The thresholds to evaluate. Matches the design doc SC-E5 row.
_THRESHOLDS = (0.60, 0.65, 0.70, 0.75, 0.80, 0.85)


def _load_probes(path: Path) -> list[dict]:
    rows = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _classify_probe(idx, query: str, threshold: float) -> str:
    """Replay one probe through the lookup pipeline with the chosen fuzzy floor.

    Returns one of: ``fast``, ``ambiguous_fast``, ``fuzzy_resolved``,
    ``ambiguous_fuzzy``, ``miss``. ``fast`` and ``ambiguous_fast`` are
    threshold-independent — they short-circuit before fuzzy fires.
    """
    candidates = idx.lookup_all(EntityType.PATHOGEN, query)
    if len(candidates) == 1:
        return "fast"
    if len(candidates) >= 2:
        return "ambiguous_fast"
    # Now into the fuzzy band — this is what threshold varies.
    hits = idx.lookup_fuzzy(query, entity_type=EntityType.PATHOGEN, threshold=threshold)
    if not hits:
        return "miss"
    top_conf = hits[0][1]
    runner_conf = hits[1][1] if len(hits) > 1 else 0.0
    near_tie = runner_conf >= top_conf - _NEAR_TIE_MARGIN
    if top_conf >= _FUZZY_RESOLVED_FLOOR and not near_tie:
        return "fuzzy_resolved"
    return "ambiguous_fuzzy"


def evaluate_threshold(idx, probes: list[dict], threshold: float) -> dict:
    """For one threshold: return per-scenario outcome counts."""
    matrix: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for probe in probes:
        outcome = _classify_probe(idx, probe["query"], threshold)
        matrix[probe["scenario"]][outcome] += 1
    return {scenario: dict(counts) for scenario, counts in matrix.items()}


def _typo_recall(matrix: dict) -> float:
    """Fraction of typo probes that lift (fuzzy_resolved OR ambiguous_fuzzy)."""
    counts = matrix.get("typo", {})
    total = sum(counts.values())
    if total == 0:
        return 0.0
    lifted = counts.get("fuzzy_resolved", 0) + counts.get("ambiguous_fuzzy", 0)
    return lifted / total


def _unresolvable_fpr(matrix: dict) -> float:
    """Fraction of UNRESOLVABLE + ADVERSARIAL probes that LIFT (false positives).

    Combines both noise-test populations to give a single FPR metric:
    - ``unresolvable`` — pure gibberish; any lift here is a clear bug.
    - ``adversarial_noise`` — biology-adjacent strings; some legitimately
      resolve (Coronaviridae IS a real family). Higher tolerance, but
      still counted as FP for headline metric.
    """
    total = 0
    lifted = 0
    for scenario in ("unresolvable", "adversarial_noise"):
        counts = matrix.get(scenario, {})
        total += sum(counts.values())
        lifted += sum(v for k, v in counts.items() if k != "miss")
    if total == 0:
        return 0.0
    return lifted / total


def _f1_balanced(recall: float, fpr: float) -> float:
    """A simple F1-style scalar where precision ≈ 1 - fpr.

    Caveat: this treats every unresolvable false positive as costing
    the same as one typo true positive. In practice operators may
    weigh false positives more heavily (they pollute the HITL queue);
    the report shows raw recall + FPR so you can apply your own weight.
    """
    precision = 1.0 - fpr
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _print_text_report(rows: list[dict]) -> None:
    print(
        f"{'threshold':>10s}  {'typo_lift':>10s}  "
        f"{'noise_pure':>10s}  {'noise_adv':>10s}  {'noise_all':>10s}  "
        f"{'recall':>8s}  {'fpr':>6s}  {'F1':>6s}"
    )
    print("-" * 92)
    for row in rows:
        m = row["matrix"]
        typo = m.get("typo", {})
        pure_noise = m.get("unresolvable", {})
        adv_noise = m.get("adversarial_noise", {})
        typo_total = sum(typo.values()) or 0
        pure_total = sum(pure_noise.values()) or 0
        adv_total = sum(adv_noise.values()) or 0
        typo_lifted = typo.get("fuzzy_resolved", 0) + typo.get("ambiguous_fuzzy", 0)
        pure_lifted = sum(v for k, v in pure_noise.items() if k != "miss")
        adv_lifted = sum(v for k, v in adv_noise.items() if k != "miss")
        all_total = pure_total + adv_total
        all_lifted = pure_lifted + adv_lifted
        print(
            f"{row['threshold']:>10.2f}  "
            f"{typo_lifted}/{typo_total:<8d}  "
            f"{pure_lifted}/{pure_total:<8d}  "
            f"{adv_lifted}/{adv_total:<8d}  "
            f"{all_lifted}/{all_total:<8d}  "
            f"{row['typo_recall']:>8.3f}  "
            f"{row['unresolvable_fpr']:>6.3f}  "
            f"{row['f1']:>6.3f}"
        )


def _recommend(rows: list[dict]) -> str:
    """Pick the threshold with the highest F1.

    Ties broken in favor of HIGHER threshold (lower FPR — operators
    suffer more from false positives than from typos that don't auto-lift).
    """
    best = max(rows, key=lambda r: (r["f1"], r["threshold"]))
    return (
        f"recommended floor: {best['threshold']:.2f} "
        f"(F1={best['f1']:.3f}, typo_recall={best['typo_recall']:.3f}, "
        f"unresolvable_fpr={best['unresolvable_fpr']:.3f})"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="calibrate_fuzzy_threshold")
    parser.add_argument(
        "--probe-path",
        type=Path,
        default=Path("tests/integration/fixtures/synonym_probe_v1.jsonl"),
    )
    parser.add_argument(
        "--dict-path",
        type=Path,
        default=None,
        help="Path to dictionary.sqlite. Falls back to env / default.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the full report as JSON instead of a human-readable table.",
    )
    args = parser.parse_args(argv)

    if not args.probe_path.exists():
        print(f"ERROR: probe set missing: {args.probe_path}", file=sys.stderr)
        return 1

    dict_path = args.dict_path or Path(
        os.environ.get(
            "APECX_SYNONYM_DICT_PATH",
            str(Path.home() / ".apecx" / "dictionary" / "dictionary.sqlite"),
        )
    )
    if not dict_path.exists():
        print(f"ERROR: dict not found at {dict_path}", file=sys.stderr)
        return 1
    configure_dictionary_path(dict_path)
    idx, err = get_dictionary_index()
    if err is not None:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1

    probes = _load_probes(args.probe_path)
    if not args.json:
        print(f"calibrating against {len(probes)} probes from {args.probe_path}\n")

    rows = []
    for t in _THRESHOLDS:
        matrix = evaluate_threshold(idx, probes, t)
        recall = _typo_recall(matrix)
        fpr = _unresolvable_fpr(matrix)
        rows.append(
            {
                "threshold": t,
                "matrix": matrix,
                "typo_recall": recall,
                "unresolvable_fpr": fpr,
                "f1": _f1_balanced(recall, fpr),
            }
        )

    if args.json:
        print(json.dumps({"probe_count": len(probes), "rows": rows}, indent=2))
    else:
        _print_text_report(rows)
        print("\n" + _recommend(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
