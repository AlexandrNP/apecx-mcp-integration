"""SolutionMemoryStep — file-backed cross-problem memory (MemFlow-style).

Implements item 3 from the post-F17 backlog: a persistent memory of
PASSING solutions, keyed by ``task_category``. Two operations:

* ``read`` (default mode): look up cached solutions for the current
  category and enrich ``code_spec`` with one as an in-context
  "Previously-passing solution:" example. Composes with the
  worked-example router pattern (F17).
* ``record``: write a new ``(category, code_source)`` pair to the
  memory file. Called by an "around" wrapper after a successful
  benchmark run.

The memory file is JSON, structured as::

    {
      "step":         ["<code1>", "<code2>", ...],
      "tool":         [...],
      "config":       [...],
      "builder":      [...],
      "mbpp_string":  [...],
      ...
    }

Per category, the most-recent solution is appended to a bounded
queue (FIFO of N max entries). Reads return the most-recent K (K=1
by default; bounded by configuration).

I/O contract — read mode
------------------------

Input::

    {"code_spec": str, "task_category": str, ...}

Output::

    {"code_spec": "<original>\\n\\nPreviously-passing solutions for
                   this category:\\n```python\\n<cached>\\n```",
     "memory_hit": bool,
     "memory_examples_used": int,
     ...passthrough}

I/O contract — record mode
--------------------------

Input::

    {"code_source": str, "task_category": str}

Output::

    {"recorded": bool, "category": str, "store_size_after": int}

Silent-failure discipline
-------------------------

* Missing or unreadable memory file → log warning, behave as empty
  store (do not raise; the scaffold should degrade gracefully).
* JSON-malformed file → same. Avoid blocking the codegen path on a
  corrupted side-channel.
* Empty store / category-not-found → emit ``memory_hit=False`` and
  pass the spec through unchanged.
* Write failure (disk full, permission) → log warning, return
  ``recorded=False``. Do not raise: cache misses are fine; cache
  corruption is also fine for benchmark sweeps.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

log = logging.getLogger(__name__)


_DEFAULT_STORE_PATH = Path(__file__).resolve().parent.parent / "_runtime" / "solution_memory.json"


class SolutionMemoryStepConfig(StepConfig):
    """Configuration for ``SolutionMemoryStep``."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)

    mode: str = Field(
        default="read",
        description=(
            "Operating mode: 'read' (exact-category lookup; default), "
            "'similarity_read' (MemFlow tier-2: cross-category top-K "
            "embedding similarity over cached solutions, with threshold "
            "fallback to exact-category 'read'), or 'record' (write a "
            "passing solution to the store)."
        ),
    )

    store_path: str | None = Field(
        default=None,
        description=(
            "Path to the JSON store file. Defaults to "
            "``composition/_runtime/solution_memory.json`` (created on "
            "first write). Override per-deployment to scope memory to "
            "a workspace or to a temp dir during tests."
        ),
    )

    max_per_category: int = Field(
        default=5,
        ge=1,
        description="FIFO bound on entries per category. Older entries are dropped.",
    )

    examples_on_read: int = Field(
        default=1,
        ge=0,
        description=(
            "Number of cached examples to surface on read (0 = lookup "
            "is a no-op but emits memory_hit telemetry)."
        ),
    )

    record_only_if_pass: bool = Field(
        default=False,
        description=(
            "In record mode, only persist when upstream signals at least "
            "one passing candidate (``voted_passes >= 1``). When True and "
            "``voted_passes`` is 0 in the input, the record is skipped with "
            "``recorded=False`` (no warning — this is the explicit gate). "
            "If ``voted_passes`` is absent from the input (e.g., upstream "
            "is a single-shot drafter without the consensus signal), the "
            "gate is bypassed (the upstream is presumed authoritative) — "
            "this lets the recorder compose with both single and multi-"
            "sample drafters."
        ),
    )

    # ---- Tier-2 (MemFlow similarity_read) configuration ----
    similarity_threshold: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description=(
            "Cosine-similarity threshold used in similarity_read mode. "
            "If the top hit's score is below this, fall back to exact-"
            "category 'read' behavior. 0.3 is a permissive default; "
            "raise to 0.5+ for stricter matching."
        ),
    )

    embedding_model: str = Field(
        default="sentence-transformers/all-mpnet-base-v2",
        description=(
            "Sentence-transformers model id for similarity_read mode. "
            "Matches the model used by the RAG e2e infrastructure for "
            "shared cache benefits. Lazy-loaded on first call."
        ),
    )

    embedding_device: str = Field(
        default="cpu",
        description=(
            "Torch device for the embedding model. 'cpu' is the default "
            "for macOS-ARM portability (mps backend has known silent-crash "
            "issues — see domain_rag/index.py header)."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data

    @model_validator(mode="after")
    def _validate_mode(self):
        if self.mode not in ("read", "similarity_read", "record"):
            raise ValueError(
                f"SolutionMemoryStepConfig: mode={self.mode!r} must be "
                f"'read', 'similarity_read', or 'record'."
            )
        return self


class SolutionMemoryStep(BaseStep):
    """Cross-problem memory of passing solutions."""

    COMPONENT_TYPE: str = "solution_memory_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return SolutionMemoryStepConfig

    @classmethod
    def extract_component_config(cls, config: SolutionMemoryStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "mode": config.mode,
            "store_path": config.store_path,
            "max_per_category": config.max_per_category,
            "examples_on_read": config.examples_on_read,
            "record_only_if_pass": config.record_only_if_pass,
            "similarity_threshold": config.similarity_threshold,
            "embedding_model": config.embedding_model,
            "embedding_device": config.embedding_device,
            "source_path": getattr(config, "source_path", None),
        }

    def _init_from_config(
        self,
        config: SolutionMemoryStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._mode: str = component_config["mode"]
        self._max_per_category: int = int(component_config["max_per_category"])
        self._examples_on_read: int = int(component_config["examples_on_read"])
        self._record_only_if_pass: bool = bool(component_config["record_only_if_pass"])
        self._similarity_threshold: float = float(component_config["similarity_threshold"])
        self._embedding_model_name: str = str(component_config["embedding_model"])
        self._embedding_device: str = str(component_config["embedding_device"])
        # Lazy-loaded on first similarity_read call.
        self._embedding_model = None

        path_str = component_config.get("store_path")
        if path_str is None:
            self._store_path: Path = _DEFAULT_STORE_PATH
        else:
            p = Path(path_str)
            self._store_path = p if p.is_absolute() else (Path.cwd() / p).resolve()

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(f"SolutionMemoryStep {self.name!r}: input_data must be a dict")

        # Trigger-envelope unwrap.
        if (
            len(input_data) == 1
            and "code_spec" not in input_data
            and "code_source" not in input_data
        ):
            (only_key,) = input_data.keys()
            if isinstance(input_data[only_key], dict):
                input_data = input_data[only_key]

        if self._mode == "read":
            return self._process_read(input_data)
        if self._mode == "similarity_read":
            return self._process_similarity_read(input_data)
        return self._process_record(input_data)

    def _process_read(self, input_data: dict[str, Any]) -> dict[str, Any]:
        spec = input_data.get("code_spec") or input_data.get("problem_prompt")
        if not isinstance(spec, str) or not spec.strip():
            raise ValueError(f"SolutionMemoryStep {self.name!r}: empty code_spec in read mode")

        category = input_data.get("task_category") or "default"
        store = self._load_store_safe()
        cached = (store.get(category) or [])[-self._examples_on_read :]

        if cached and self._examples_on_read > 0:
            examples_block = "\n\n".join(f"```python\n{c.strip()}\n```" for c in cached)
            enriched = (
                f"{spec.strip()}\n\nPreviously-passing solutions for this "
                f"category:\n\n{examples_block}\n"
            )
        else:
            enriched = spec

        return {
            "code_spec": enriched,
            "memory_hit": bool(cached) and self._examples_on_read > 0,
            "memory_examples_used": min(len(cached), self._examples_on_read),
            "task_category": category,
            "entry_point": input_data.get("entry_point"),
            "test_hint": input_data.get("test_hint"),
            "function_signature": input_data.get("function_signature"),
        }

    def _process_similarity_read(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """MemFlow tier-2: cross-category top-K embedding-similarity read.

        Algorithm:
        1. Flatten the store: list of (category, code) tuples across ALL buckets.
        2. Encode the current code_spec as a query embedding.
        3. Encode every cached code as a value embedding.
        4. Cosine-similarity rank; take top-K (where K = examples_on_read).
        5. If top-1 similarity < similarity_threshold, FALL BACK to exact-
           category read (tier-1). This keeps the lookup conservative: a
           weak similarity match is worse than no enrichment.

        Optimization deferred: embeddings are recomputed on every call.
        For n=10..100 cached entries this is ~50-500ms per call. For
        production traffic >>1000 entries, cache the value-embeddings
        keyed by store content hash.
        """
        spec = input_data.get("code_spec") or input_data.get("problem_prompt")
        if not isinstance(spec, str) or not spec.strip():
            raise ValueError(
                f"SolutionMemoryStep {self.name!r}: empty code_spec in similarity_read mode"
            )

        category = input_data.get("task_category") or "default"
        store = self._load_store_safe()

        # Flatten across categories.
        all_entries: list[tuple[str, str]] = []
        for cat, codes in store.items():
            for code in codes:
                if isinstance(code, str) and code.strip():
                    all_entries.append((cat, code))

        # If the store is empty, behave like exact-category read (which
        # will also be a no-op miss but produce consistent telemetry).
        if not all_entries or self._examples_on_read <= 0:
            return self._fallback_to_exact_read(
                spec=spec, category=category, input_data=input_data, reason="empty_store"
            )

        try:
            model = self._load_embedding_model()
            import numpy as np  # noqa: PLC0415

            query_vec = model.encode(
                [spec],
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )[0].astype("float32")
            value_vecs = model.encode(
                [c for _, c in all_entries],
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            ).astype("float32")
            # Cosine similarity = dot product because both are L2-normalized.
            scores = value_vecs @ query_vec
            top_idx = np.argsort(-scores)[: self._examples_on_read]
            top_score = float(scores[top_idx[0]]) if len(top_idx) else 0.0
        except Exception as e:  # noqa: BLE001
            # Embedding model failure must NOT break the codegen path.
            # Degrade to tier-1 exact-category lookup.
            log.warning(
                "SolutionMemoryStep %r: embedding failed in similarity_read "
                "(%s); falling back to exact-category read",
                self.name,
                e,
            )
            return self._fallback_to_exact_read(
                spec=spec,
                category=category,
                input_data=input_data,
                reason=f"embedding_error:{type(e).__name__}",
            )

        # Threshold gate: weak top-1 match -> fall back to tier-1.
        if top_score < self._similarity_threshold:
            return self._fallback_to_exact_read(
                spec=spec,
                category=category,
                input_data=input_data,
                reason=f"below_threshold:{top_score:.3f}<{self._similarity_threshold}",
            )

        picked = [all_entries[i] for i in top_idx]
        examples_block = "\n\n".join(
            f"```python\n{code.strip()}\n```  # from category={cat!r}, similarity={float(scores[i]):.3f}"
            for (cat, code), i in zip(picked, top_idx, strict=True)
        )
        enriched = (
            f"{spec.strip()}\n\nPreviously-passing solutions similar to this "
            f"problem (cross-category):\n\n{examples_block}\n"
        )

        log.info(
            "SolutionMemoryStep %r: similarity_read top-K=%d, top_score=%.3f, fallback=False",
            self.name,
            len(picked),
            top_score,
        )

        return {
            "code_spec": enriched,
            "memory_hit": True,
            "memory_examples_used": len(picked),
            "memory_mode": "similarity",
            "memory_top_score": top_score,
            "memory_categories": [cat for cat, _ in picked],
            "task_category": category,
            "entry_point": input_data.get("entry_point"),
            "test_hint": input_data.get("test_hint"),
            "function_signature": input_data.get("function_signature"),
        }

    def _fallback_to_exact_read(
        self,
        *,
        spec: str,
        category: str,
        input_data: dict[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        """Tier-2 graceful degradation to tier-1 (exact-category) lookup."""
        log.info(
            "SolutionMemoryStep %r: similarity_read falling back to exact-category (%s)",
            self.name,
            reason,
        )
        out = self._process_read({**input_data, "code_spec": spec, "task_category": category})
        out["memory_mode"] = "exact_fallback"
        out["memory_fallback_reason"] = reason
        return out

    def _load_embedding_model(self):
        """Lazy-load sentence-transformers model on first similarity_read call.

        Import order is load-bearing on macOS-ARM: ``sentence_transformers``
        MUST import BEFORE ``faiss`` (which is loaded elsewhere in this
        process via the RAG infra). See domain_rag/index.py:1-7.
        """
        if self._embedding_model is None:
            from sentence_transformers import SentenceTransformer  # noqa: PLC0415

            self._embedding_model = SentenceTransformer(
                self._embedding_model_name,
                device=self._embedding_device,
            )
        return self._embedding_model

    def _process_record(self, input_data: dict[str, Any]) -> dict[str, Any]:
        code = input_data.get("code_source")
        category = input_data.get("task_category") or "default"
        if not isinstance(code, str) or not code.strip():
            log.warning(
                "SolutionMemoryStep %r: record skipped — empty code_source",
                self.name,
            )
            return {"recorded": False, "category": category, "store_size_after": 0}

        # Optional gate: only record when upstream consensus signals a pass.
        # ``voted_passes`` is emitted by ConsensusAggregatorStep. When absent
        # (single-shot drafter), the gate is bypassed — see field docstring.
        if self._record_only_if_pass and "voted_passes" in input_data:
            voted = input_data.get("voted_passes") or 0
            if int(voted) < 1:
                return {
                    "recorded": False,
                    "category": category,
                    "store_size_after": len((self._load_store_safe()).get(category, [])),
                }

        store = self._load_store_safe()
        bucket = store.setdefault(category, [])
        bucket.append(code.strip())
        # FIFO bound.
        if len(bucket) > self._max_per_category:
            bucket[:] = bucket[-self._max_per_category :]
        ok = self._save_store_safe(store)
        log.info(
            "SolutionMemoryStep %r: recorded=%s, category=%r, store_size=%d",
            self.name,
            ok,
            category,
            len(bucket),
        )
        return {
            "recorded": ok,
            "category": category,
            "store_size_after": len(bucket),
        }

    def _load_store_safe(self) -> dict[str, list[str]]:
        if not self._store_path.is_file():
            return {}
        try:
            text = self._store_path.read_text(encoding="utf-8")
            data = json.loads(text)
            if not isinstance(data, dict):
                log.warning(
                    "SolutionMemoryStep %r: store at %s is not a dict; ignoring",
                    self.name,
                    self._store_path,
                )
                return {}
            return data
        except (OSError, json.JSONDecodeError) as e:
            log.warning(
                "SolutionMemoryStep %r: store load failed (%s); treating as empty",
                self.name,
                e,
            )
            return {}

    def _save_store_safe(self, store: dict[str, list[str]]) -> bool:
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            self._store_path.write_text(json.dumps(store, indent=2), encoding="utf-8")
            return True
        except OSError as e:
            log.warning(
                "SolutionMemoryStep %r: store write failed: %s",
                self.name,
                e,
            )
            return False


__all__ = ["SolutionMemoryStep", "SolutionMemoryStepConfig"]
