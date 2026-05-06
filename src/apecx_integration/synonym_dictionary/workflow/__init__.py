"""Nanobrain-wrapped end-to-end IRI resolution workflow.

This subpackage wraps the existing plain-Python ``lookup_entity`` /
``DictionaryIndex`` infrastructure as a real nanobrain workflow:

- :class:`NormalizeEntityRecordsStep` — normalizes incoming records'
  ``surface_form`` (lowercase + strip; preserves the original under
  ``_original_surface_form``).
- :class:`ResolveIRIStep` — calls ``lookup_entity`` per record and writes
  the canonical-IRI fields back onto the record.

Both steps are constructed via ``from_config`` and live behind the
top-level ``iri_resolution_workflow.yml`` so this pipeline can be
composed with other nanobrain workflows or plugged into apecx-harvesters'
ingest stage as a ``Transform``.
"""

from apecx_integration.synonym_dictionary.workflow.normalize_step import (
    NormalizeEntityRecordsStep,
)
from apecx_integration.synonym_dictionary.workflow.resolve_step import (
    ResolveIRIStep,
)
from apecx_integration.synonym_dictionary.workflow.workflow import (
    IRIResolutionWorkflow,
)

__all__ = [
    "IRIResolutionWorkflow",
    "NormalizeEntityRecordsStep",
    "ResolveIRIStep",
]
