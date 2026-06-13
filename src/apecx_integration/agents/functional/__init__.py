"""Residue-level functional/immunological annotation clients (E3-3).

Three async HTTP clients (modeled on ``synonym_dictionary.ols_client.OLSClient``)
that supply the FunctionalValidationStep with REAL residue-level annotation:

- :mod:`sifts_client` — PDB→UniProt accession + author-numbering residue bridge.
- :mod:`uniprot_client` — residue-level sequence features + the canonical sequence.
- :mod:`iedb_client` — known B/T-cell epitopes (linear sequences) for an antigen.

The cross-check orchestration + numbering bridge live in :mod:`residue_annotation`.
"""
