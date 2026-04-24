"""T03 AC3: 80% top-5 recall on the 20-query ground-truth suite.

Contract (implementation_plan.md §T03 AC3):

    80% top-5 recall on the 20-query test suite.

This test builds a ``ComponentIndex`` against the violin_bvbrc
manifest, runs every query in ``queries.yaml``, and asserts the
expected component appears in the top-5 hits for >=80% of queries.

Skips
-----
Skips when ``nanobrain.lightweight.component_index`` is not importable
(expected outside a full dev install) or when the embedding model is
not cached locally (cold CI).

Authoring trust
---------------
See the disclosure block at the top of ``queries.yaml`` — curation
bias is real. The 80% target is a FLOOR, not ceiling. A second
curator should re-review before production release.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

try:
    from nanobrain.lightweight.component_index import ComponentIndex
    _NB_IMPORT_ERROR: str | None = None
except ImportError as exc:  # pragma: no cover - import-path survey
    ComponentIndex = None  # type: ignore[assignment]
    _NB_IMPORT_ERROR = str(exc)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = (
    REPO_ROOT
    / "src"
    / "apecx_integration"
    / "composition"
    / "workflows"
    / "violin_bvbrc"
    / "manifest.yml"
)
QUERIES_YAML = Path(__file__).parent / "queries.yaml"

MIN_RECALL_AT_5: float = 0.80


def _model_cache_present() -> bool:
    cache_dir = Path.home() / ".cache" / "huggingface" / "hub"
    target = cache_dir / "models--sentence-transformers--all-mpnet-base-v2"
    return target.is_dir()


SKIP_NB_MISSING = (
    f"nanobrain.lightweight.component_index not importable: "
    f"{_NB_IMPORT_ERROR}. Set PYTHONPATH to include the nanobrain repo."
)
SKIP_MODEL_MISSING = (
    "all-mpnet-base-v2 not cached — cold CI. Set "
    "HUGGINGFACE_HUB_CACHE to a warm cache or pre-pull the model."
)


@pytest.fixture(scope="module")
def index():
    idx = ComponentIndex()
    idx.rebuild(manifest_paths=[MANIFEST], library_version="0.1.0-test")
    return idx


@pytest.mark.skipif(ComponentIndex is None, reason=SKIP_NB_MISSING)
@pytest.mark.skipif(not _model_cache_present(), reason=SKIP_MODEL_MISSING)
def test_top5_recall_meets_ac3_floor(index):
    suite = yaml.safe_load(QUERIES_YAML.read_text(encoding="utf-8"))
    cases = suite["cases"]
    assert len(cases) == 20, (
        f"queries.yaml must have exactly 20 cases per AC3; has {len(cases)}"
    )

    hits: list[tuple[str, str, list[str]]] = []
    misses: list[tuple[str, str, list[str]]] = []
    for case in cases:
        q: str = case["query"]
        expected: str = case["top_expected"]
        results = index.search(q, k=5)
        result_names = [r.name for r in results]
        if expected in result_names:
            hits.append((q, expected, result_names))
        else:
            misses.append((q, expected, result_names))

    recall = len(hits) / len(cases)
    # Detailed miss listing — surfaces exactly which queries failed so
    # description tuning has something concrete to chew on, instead of
    # "recall was 65%" with no actionable detail.
    assert recall >= MIN_RECALL_AT_5, (
        f"AC3 floor violated: recall@5 = {recall:.2%} < "
        f"{MIN_RECALL_AT_5:.0%}. "
        f"{len(misses)} misses:\n"
        + "\n".join(
            f"  - expected {expected!r} for {q!r}; got {got}"
            for q, expected, got in misses
        )
    )


@pytest.mark.skipif(ComponentIndex is None, reason=SKIP_NB_MISSING)
@pytest.mark.skipif(not _model_cache_present(), reason=SKIP_MODEL_MISSING)
def test_top1_recall_reported_as_informational(index, capsys):
    """Diagnostic: how often does the expected component rank #1?

    Not a hard assertion — AC3 specifies top-5, not top-1 — but the
    top-1 rate informs whether tightening retrieval_k to 5 (or lower)
    in the composer would regress quality.
    """
    suite = yaml.safe_load(QUERIES_YAML.read_text(encoding="utf-8"))
    cases = suite["cases"]
    top1 = 0
    for case in cases:
        results = index.search(case["query"], k=1)
        if results and results[0].name == case["top_expected"]:
            top1 += 1
    rate = top1 / len(cases)
    # Write to captured stdout so `pytest -s` surfaces it.
    print(f"\n[T03 diagnostic] top-1 recall: {rate:.2%} ({top1}/{len(cases)})")
    # No assertion — diagnostic only. Guard against accidental regression
    # via a very loose floor.
    assert rate >= 0.40, (
        f"top-1 recall collapsed to {rate:.2%} — descriptions may have "
        "regressed or the embedding model changed."
    )
