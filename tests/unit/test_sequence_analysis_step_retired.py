"""SequenceAnalysisStep is RETIRED — it must FAIL LOUD, never fabricate conservation.

Regression for the mock-in-production finding (2026-06-12): the step used to write
``"ATCGATCG"*20`` placeholder sequences + copy its input as a "mock alignment", producing fake
conserved regions. It now raises. Real conserved-site analysis is the viral_conserved_sites
workflow (BvbrcProteinFastaStep → LocalMafftAlignStep → ConservationScoreStep).
"""

from __future__ import annotations

import asyncio

import pytest

from apecx_integration.composition.steps.sequence_analysis_step import SequenceAnalysisStep


def test_retired_step_fails_loud(tmp_path):
    p = tmp_path / "seq.yml"
    p.write_text("name: retired_seq\n")
    step = SequenceAnalysisStep.from_config(str(p))
    with pytest.raises(NotImplementedError, match="RETIRED"):
        asyncio.run(
            step.process(
                {
                    "query_data": {
                        "virus_name": "Chikungunya virus",
                        "protein_target": "E1",
                        "analysis_type": "conservation",
                    }
                }
            )
        )
