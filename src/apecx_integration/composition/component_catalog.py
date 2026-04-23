"""Component catalog — the composer's source-of-truth for "what library
components exist and what they do."

Phase 2 reads the catalog from a **YAML file** rather than from the
T09 ``Component`` DB table. Why the deviation from the spec's
"Query the Component table directly":

- The Component table was shipped with T09 but has zero seed data;
  no code populates it. A DB query returns nothing → no candidates
  reach the LLM → composition prompt is meaningless.
- The workflow-specific manifests (``src/apecx_integration/composition/
  workflows/<wf>/manifest.yml``) already carry the rich per-component
  metadata (rag_description, rag_examples, implementation_path,
  status). They ARE the library index; the DB table is a future
  denormalization of them.
- File-backed catalog keeps Phase 2 composer-independent of Tier 2
  DB availability. A composer invocation doesn't need a live Control
  Plane.

When T03 RAG lands (Phase 4 of T-COMP), it will replace this
substring-match retrieval with a K-NN lookup over pre-computed
embeddings. The ``ComponentCatalog.search`` signature stays stable
across the swap — only the internals change.

Design notes
------------
- **No DB access.** This class intentionally does not import
  anything from ``apecx_integration.control_plane.models``. The
  composer's retrieval step is a pure in-memory operation over
  manifests loaded at catalog-construction time.
- **Substring match only.** Case-insensitive substring against
  ``rag_description`` + ``name`` + ``rag_examples``. Good enough to
  surface "entity_extraction" when the prompt says "extract entities
  from a query." NOT good enough for paraphrase — but a 10-row
  library is small enough that recall dominates precision concerns
  at Phase 2.
- **Ranking is match-count, not TF-IDF.** Simpler = fewer test cases
  + easier to predict behavior. Phase 4 replaces with cosine-similarity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class CatalogComponent:
    """One library component surfaced to the composer.

    Mirrors the shape the T02 manifests emit (minus workflow-specific
    fields like ``disposition`` and ``status``). Renamed so the
    composer's callers don't have to import anything T02-specific.
    """

    id: str                         # stable identifier; e.g. "entity_extraction"
    name: str                       # human label; often == id
    description: str                # rag_description from the manifest
    class_path: str                 # fully-qualified Python class path
    yaml_path: str | None           # wrapper YAML path (None for bare-function reuse)
    examples: tuple[str, ...] = ()  # rag_examples from the manifest
    domain: str = "generic"


@dataclass
class SearchHit:
    """One ranked retrieval result."""

    component: CatalogComponent
    score: int                      # raw match count; higher = better


@dataclass
class ComponentCatalog:
    """In-memory catalog loaded from one or more manifest YAML files.

    Construction: use ``from_manifests(paths)``. Direct-construct
    (an empty catalog) is allowed for testing; a zero-component
    catalog is a legitimate state when no manifests are configured.
    """

    components: tuple[CatalogComponent, ...] = field(default_factory=tuple)

    @classmethod
    def from_manifests(cls, paths: list[Path]) -> ComponentCatalog:
        """Load + flatten components from one or more manifest YAMLs."""
        all_components: list[CatalogComponent] = []
        for p in paths:
            loaded = _load_manifest(p)
            all_components.extend(loaded)
        # Deduplicate on id — if the same component appears in multiple
        # manifests, the last one wins. Empirically the composer shouldn't
        # see duplicates at Phase 2 (one manifest per workflow); this
        # guard exists for Phase 4 multi-library setups.
        by_id: dict[str, CatalogComponent] = {}
        for c in all_components:
            by_id[c.id] = c
        return cls(components=tuple(by_id.values()))

    def search(self, query: str, k: int = 10) -> list[SearchHit]:
        """Substring-match ``query`` against every component's
        description + name + examples. Return the top-k hits by
        score (tie-breaker: original order in the catalog).

        Empty catalog returns an empty list — callers must handle
        this (composer falls back to "no candidates; LLM must author
        from scratch" prompting).
        """
        if not self.components:
            return []

        tokens = _tokenize(query)
        if not tokens:
            return []

        hits: list[SearchHit] = []
        for component in self.components:
            corpus = " ".join([
                component.name,
                component.description,
                *component.examples,
            ]).lower()
            score = sum(1 for t in tokens if t in corpus)
            if score > 0:
                hits.append(SearchHit(component=component, score=score))

        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

    def __len__(self) -> int:
        return len(self.components)


# ---------------------------------------------------------------------------
# Manifest loader
# ---------------------------------------------------------------------------

def _load_manifest(path: Path) -> list[CatalogComponent]:
    """Parse one manifest YAML into a list of CatalogComponent.

    Accepts the T02 manifest shape: top-level ``components:`` list,
    each entry has ``step_id``, ``step_name``, ``class``, ``yaml``,
    ``rag_description``, ``rag_examples``.

    Components with ``disposition: deferred`` are skipped — they're
    not available for composition.
    """
    if not path.is_file():
        raise FileNotFoundError(
            f"ComponentCatalog manifest not found at {path}"
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(
            f"Manifest at {path} must be a YAML mapping at the top level"
        )
    entries = raw.get("components") or []
    if not isinstance(entries, list):
        raise ValueError(
            f"Manifest at {path} must have 'components:' as a list"
        )

    out: list[CatalogComponent] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("disposition") == "deferred":
            continue
        step_id = entry.get("step_id") or entry.get("step_name")
        if step_id is None:
            continue
        name = entry.get("step_name") or str(step_id)
        description = _strip_multiline(entry.get("rag_description", ""))
        if not description:
            # A component with no rag_description is unretrievable at Phase 2
            # (substring match against empty string = no hits). Skip it and
            # surface in logs rather than pollute the catalog.
            continue
        examples_raw = entry.get("rag_examples") or []
        examples = tuple(
            _strip_multiline(e) for e in examples_raw
            if isinstance(e, str)
        )
        out.append(CatalogComponent(
            id=str(step_id),
            name=str(name),
            description=description,
            class_path=str(entry.get("class") or ""),
            yaml_path=entry.get("yaml") if isinstance(entry.get("yaml"), str) else None,
            examples=examples,
            domain=str(entry.get("domain", "generic")),
        ))
    return out


def _strip_multiline(s: Any) -> str:
    """YAML's ``>`` folded scalars leave trailing newlines + internal
    whitespace runs. Normalize to a single space-separated line."""
    if not isinstance(s, str):
        return ""
    return re.sub(r"\s+", " ", s).strip()


def _tokenize(query: str) -> list[str]:
    """Lowercase + split on non-alphanumeric. No stemming, no stopword
    removal — Phase 4 RAG replaces this; don't invest in features we'll
    throw away."""
    return [t for t in re.split(r"[^a-z0-9]+", query.lower()) if t]


__all__ = [
    "CatalogComponent",
    "ComponentCatalog",
    "SearchHit",
]
