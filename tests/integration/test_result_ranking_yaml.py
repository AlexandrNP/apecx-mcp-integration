"""T02 Phase 4 loadability: Step 7 result_ranking wrapper YAML loads
via the nanobrain ``ResultCollectionStep.from_config`` pathway.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nanobrain.library.workflows.viral_protein_analysis.steps.result_collection_step import (
    ResultCollectionStep,
)

pytestmark = pytest.mark.integration


def test_result_ranking_wrapper_yaml_loads() -> None:
    path = (
        Path(__file__).resolve().parents[1].parent
        / "src"
        / "apecx_integration"
        / "composition"
        / "workflows"
        / "violin_bvbrc"
        / "steps"
        / "result_ranking.yml"
    )
    assert path.is_file(), path
    step = ResultCollectionStep.from_config(str(path))
    assert step.name == "result_ranking"
