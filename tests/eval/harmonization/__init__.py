"""Non-circular cross-index harmonization precision/recall evaluation.

Measures the LIVE shipped harmonized_search across the 140-query prior corpus, judging record
relevance from signals the retrieval filter did NOT use (see judges.py). Read-only: any bug it
surfaces is a separate /feature fix. See HARMONIZATION_PRECISION_FINDINGS.md for results.
"""
