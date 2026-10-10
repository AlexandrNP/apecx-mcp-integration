"""Real-PubMed check that the A1 question term retrieves the on-topic 2020 RRV E2 paper.

Runs the exact term ``literature_qa`` builds for the run29 A1 question through the real
harvest path (``harvest_pubmed``, no mocks) with the pool size the acceptance run used
(``max_papers=300``), and asserts PMID 32365139 falls inside that window. PMID 32365139 is
"Human monoclonal antibodies against Ross River virus target epitopes within the E2 protein
and protect against disease" (PubMed, 2020 May). Skips when PubMed is unreachable.
"""

from __future__ import annotations

import httpx
import pytest

from apecx_integration.agents.literature.harvest import harvest_pubmed
from apecx_integration.mcp_surface.tools.literature_qa import _question_term

pytestmark = pytest.mark.integration

A1_QUESTION = (
    "List the PubMed IDs of studies reporting Ross River virus E2 glycoprotein epitopes "
    "recognised by neutralising antibodies, with the PMID for each."
)
ON_TOPIC_PMID = "32365139"
ACCEPTANCE_POOL = 300


def test_a1_term_retrieves_on_topic_rrv_e2_paper_within_pool():
    term = _question_term("Ross River virus", A1_QUESTION)
    try:
        records = harvest_pubmed(term, max_papers=ACCEPTANCE_POOL)
    except (httpx.HTTPError, OSError) as exc:
        pytest.skip(f"PubMed unreachable: {exc}")
    pmids = [str(r["pmid"]) for r in records]
    assert len(pmids) >= 200
    assert ON_TOPIC_PMID in pmids
