"""LLM composer — Phase 2 (T-COMP per docs/composer_task_spec.md).

Phase 2 fills in ``Composer.compose()``:

    prompt + catalog search
        → build LLM messages (3 prompt files + candidates dump + user prompt)
        → call ``apecx_db_integration.agent._build_chat_llm``
        → parse ``yaml`` + optional ``novel_python`` fenced blocks
        → T13 scanner over novel Python (reject on violation)
        → return ``ComposedWorkflow`` (NOT persisted — that's Phase 3)

Exit criterion (spec §6 P2): a fixture prompt produces non-empty
``yaml_bytes`` that loads via ``Workflow.from_config(...)``.

**Deviation from spec §6 P2, documented**: the spec said "Query the
Component table directly." The T09 Component DB table has zero seed
data (no code populates it), so a DB query returns nothing. Phase 2
reads components from ``ComponentCatalog.from_manifests([paths])``
instead — paths come from ``ComposerConfig.component_catalog_paths``.
When T03 RAG lands it will replace the catalog's substring-match
search with embedding K-NN; the ``Composer`` interface doesn't change.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from apecx_integration.composition.component_catalog import (
    ComponentCatalog,
    SearchHit,
)
from apecx_integration.composition.composer_schemas import (
    ComposedWorkflow,
    ComposerConfig,
    CompositionSummary,
)
from apecx_integration.composition.sandbox import (
    ImportScanner,
    ScanViolation,
    load_whitelist,
)

log = logging.getLogger(__name__)


REQUIRED_PROMPT_FILES: tuple[str, ...] = (
    "system.md",
    "composition_bias.md",
    "novel_python_flagging.md",
)

# Keys the composer consumes internally from ``context`` — not included
# in the LLM-visible "Additional context" block. ``run_id`` is the
# ArtifactStore's attribution target (Phase 3); more plumbing keys may
# land in Phase 4+ (retrieval_override, etc.).
_INTERNAL_CONTEXT_KEYS: frozenset[str] = frozenset({"run_id"})


class ComposerConfigurationError(ValueError):
    """Raised when a composer config is structurally wrong."""


class ComposerResponseError(ValueError):
    """Raised when the LLM response can't be parsed into a workflow.

    Separate from ComposerConfigurationError so callers can
    distinguish "operator misconfigured the composer" from "LLM
    emitted unparseable output."
    """


class Composer:
    """LLM-backed workflow composer (T-COMP).

    Phase 2: retrieval + LLM call + parse + scanner. ``compose()``
    returns a ``ComposedWorkflow`` but does NOT persist via
    ``ArtifactStore``.

    Phase 3 (2026-04-23): optional ``artifact_store`` injection in
    ``__init__``. If provided AND the caller passes
    ``context={"run_id": ...}`` to ``compose()``, the generated YAML
    is persisted via ``ArtifactStore.store()`` and the returned
    ``ComposedWorkflow.artifact_id`` is the Artifact row's UUID.
    When either is missing, Phase-2 behavior is preserved: a uuid4
    is synthesized and no persistence happens (useful for tests
    that don't want to stand up a DB).
    """

    def __init__(
        self,
        config: ComposerConfig,
        *,
        llm_factory=None,
        artifact_store=None,
    ) -> None:
        """
        Args:
            config: ComposerConfig loaded from YAML.
            llm_factory: Optional callable returning a LangChain-compatible
                LLM client. Defaults to
                ``apecx_db_integration.agent._build_chat_llm``. Tests
                inject a placeholder via this seam — see
                ``tests/integration/test_composer_phase2.py``.
            artifact_store: Optional ``ArtifactStore`` (T11 primitive).
                When provided + ``compose(context={"run_id": ...})``,
                the generated YAML is persisted and the returned
                ``ComposedWorkflow.artifact_id`` is the Artifact row's
                UUID. When ``None`` (default), Phase-2 behavior is
                preserved: uuid4 synthesized, no persistence.
        """
        self._config = config
        self._prompts: dict[str, str] = self._load_prompts(config.prompt_dir)
        self._catalog = ComponentCatalog.from_manifests(
            list(config.component_catalog_paths)
        )
        self._llm_factory = llm_factory or _default_llm_factory
        self._artifact_store = artifact_store
        log.info(
            "Composer initialized (Phase %s): library=%s llm=%s prompts=%d "
            "components=%d persist=%s",
            "3" if artifact_store is not None else "2",
            config.library_version,
            config.llm_model,
            len(self._prompts),
            len(self._catalog),
            artifact_store is not None,
        )

    @classmethod
    def from_config(cls, config_path: str | Path) -> Composer:
        """Load a ``ComposerConfig`` from YAML and build the composer."""
        path = Path(config_path)
        if not path.is_file():
            raise ComposerConfigurationError(
                f"composer config not found at {path}"
            )
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ComposerConfigurationError(
                f"composer config at {path} must be a YAML mapping, got "
                f"{type(raw).__name__}"
            )

        # Resolve prompt_dir relative to the config file if it's not absolute.
        prompt_dir_raw = raw.get("prompt_dir")
        if prompt_dir_raw is None:
            raise ComposerConfigurationError(
                "composer config missing required 'prompt_dir'"
            )
        prompt_dir = Path(prompt_dir_raw)
        if not prompt_dir.is_absolute():
            prompt_dir = (path.parent / prompt_dir).resolve()
        raw["prompt_dir"] = prompt_dir

        # Same path-resolution treatment for the optional catalog + whitelist
        # paths added in Phase 2.
        raw_catalogs = raw.get("component_catalog_paths") or []
        if not isinstance(raw_catalogs, list):
            raise ComposerConfigurationError(
                f"component_catalog_paths must be a list, got {type(raw_catalogs).__name__}"
            )
        resolved_catalogs: list[Path] = []
        for entry in raw_catalogs:
            p = Path(entry)
            if not p.is_absolute():
                p = (path.parent / p).resolve()
            resolved_catalogs.append(p)
        raw["component_catalog_paths"] = resolved_catalogs

        whitelist_raw = raw.get("sandbox_whitelist_path")
        if whitelist_raw is not None:
            wlp = Path(whitelist_raw)
            if not wlp.is_absolute():
                wlp = (path.parent / wlp).resolve()
            raw["sandbox_whitelist_path"] = wlp

        try:
            config = ComposerConfig(**raw)
        except Exception as exc:
            raise ComposerConfigurationError(
                f"composer config at {path} failed validation: {exc}"
            ) from exc

        return cls(config)

    @property
    def config(self) -> ComposerConfig:
        return self._config

    @property
    def prompts(self) -> dict[str, str]:
        return dict(self._prompts)

    @property
    def catalog(self) -> ComponentCatalog:
        return self._catalog

    async def compose(
        self,
        prompt: str,
        context: dict[str, Any] | None = None,
    ) -> ComposedWorkflow:
        """Compose a workflow YAML from a natural-language prompt.

        Phase-2 pipeline: retrieve → build messages → LLM call → parse
        → scanner → return. Does NOT persist via ArtifactStore (Phase 3).

        Raises:
            ComposerResponseError: the LLM response could not be parsed
                as a ``yaml``+optional-``novel_python`` fenced pair.
            ScanViolation: novel Python failed the T13 whitelist /
                banned-construct check.
        """
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(
                f"compose() requires a non-empty prompt string; got "
                f"{type(prompt).__name__}"
            )

        # 1. Retrieve candidate components
        hits = self._catalog.search(prompt, k=self._config.retrieval_k)
        log.info("Composer retrieval: %d hits for prompt", len(hits))

        # 2. Build LLM messages
        system_prompt = self._build_system_prompt()
        user_prompt = self._build_user_prompt(prompt, hits, context)

        # 3. Call LLM via the injected factory
        from langchain_core.messages import HumanMessage, SystemMessage
        llm = self._llm_factory(
            temperature=self._config.temperature,
            max_tokens=self._config.max_tokens,
            model=self._config.llm_model,
            base_url=self._config.llm_base_url,
        )
        response = llm.invoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ])
        raw_content = getattr(response, "content", str(response))

        # 4. Parse fenced blocks
        yaml_text, novel_python = _parse_response(raw_content)

        # 5. T13 scanner over novel Python (if any)
        if novel_python and self._config.sandbox_whitelist_path is not None:
            whitelist = load_whitelist(self._config.sandbox_whitelist_path)
            scanner = ImportScanner(whitelist=whitelist)
            for _step_id, source in novel_python.items():
                result = scanner.scan(source)
                if not result.ok:
                    raise ScanViolation(result)

        # 6. Sanity-parse the YAML so we catch obviously-broken output
        #    before returning. If the LLM emitted un-parseable YAML, that's
        #    a composer-response error, not a caller's problem.
        try:
            workflow_dict = yaml.safe_load(yaml_text)
        except yaml.YAMLError as exc:
            raise ComposerResponseError(
                f"LLM response's yaml block failed to parse: {exc}"
            ) from exc
        if not isinstance(workflow_dict, dict):
            raise ComposerResponseError(
                f"LLM response's yaml block must be a mapping at top level, "
                f"got {type(workflow_dict).__name__}"
            )

        # 7. Assemble ComposedWorkflow.
        yaml_bytes = yaml_text.encode("utf-8")
        reused = [h.component.id for h in hits if h.component.id in (workflow_dict.get("steps") or {})]
        summary = CompositionSummary(
            steps_reused=len(reused),
            steps_generated=len(novel_python),
            steps_swapped=0,
            summary_sentence=(
                f"Reused {len(reused)} library component(s); "
                f"generated {len(novel_python)} novel Python step(s)."
            ),
            review_notes=tuple(
                f"novel Python step: {k}" for k in novel_python
            ),
        )
        llm_model_version_hash = hashlib.sha256(
            self._config.llm_model.encode("utf-8")
        ).hexdigest()

        # 8. Persist via ArtifactStore when both an injected store AND a
        # run_id context are available. Otherwise stay on the Phase-2
        # in-memory path (synthesize uuid4, no DB write). This split
        # preserves compatibility for tests that don't want DB setup.
        artifact_id = self._persist_or_synthesize(
            yaml_bytes=yaml_bytes,
            prompt=prompt,
            context=context,
            composition_summary=summary,
            llm_model_version_hash=llm_model_version_hash,
        )

        return ComposedWorkflow(
            artifact_id=artifact_id,
            yaml_bytes=yaml_bytes,
            novel_python=novel_python,
            composition_summary=summary,
            retrieved_components=tuple(h.component.id for h in hits),
            llm_model=self._config.llm_model,
            llm_model_version_hash=llm_model_version_hash,
        )

    # ---- internals ---------------------------------------------------------

    def _persist_or_synthesize(
        self,
        *,
        yaml_bytes: bytes,
        prompt: str,
        context: dict[str, Any] | None,
        composition_summary: CompositionSummary,
        llm_model_version_hash: str,
    ):
        """Either persist via ArtifactStore and return its Artifact UUID,
        or synthesize a uuid4 (Phase-2 compat).

        Persistence requires BOTH:
          1. An ``ArtifactStore`` was injected at Composer construction.
          2. The caller passed ``context={"run_id": <UUID>}`` to compose().

        Rationale for the split: tests that only want to exercise the
        LLM-call + parse path don't need to spin up a migrated SQLite
        DB. Production always has both and always persists.
        """
        from uuid import UUID as UUIDType

        if self._artifact_store is None:
            return uuid4()

        run_id_raw = (context or {}).get("run_id")
        if run_id_raw is None:
            # Store injected but caller didn't provide run_id. This is
            # a valid configuration for smoke-type usage; warn once
            # and fall back to the Phase-2 path.
            log.warning(
                "Composer has an ArtifactStore but compose() was called "
                "without context['run_id']; skipping persistence, "
                "synthesizing uuid4 instead. For production, pass "
                "context={'run_id': <run uuid>}."
            )
            return uuid4()

        run_id = run_id_raw if isinstance(run_id_raw, UUIDType) else UUIDType(str(run_id_raw))

        # Lazy import so non-persisting tests don't need the full
        # control_plane dependency chain just to import Composer.
        from apecx_integration.composition.artifact_store import (
            GenerationMetadata,
        )
        from apecx_integration.control_plane.schemas.enums import ArtifactKind

        generated_metadata = GenerationMetadata(
            source_prompt=prompt,
            library_version=self._config.library_version,
            llm_model=self._config.llm_model,
            llm_model_version_hash=llm_model_version_hash,
            composition_summary={
                "steps_reused": composition_summary.steps_reused,
                "steps_generated": composition_summary.steps_generated,
                "steps_swapped": composition_summary.steps_swapped,
                "summary_sentence": composition_summary.summary_sentence,
            },
        )
        artifact = self._artifact_store.store(
            content=yaml_bytes,
            kind=ArtifactKind.GENERATED_WORKFLOW,
            run_id=run_id,
            mime_type="application/yaml",
            generated_metadata=generated_metadata,
        )
        log.info(
            "Composer persisted artifact %s (content_hash=%s, len=%d)",
            artifact.id,
            artifact.content_hash[:16],
            artifact.size_bytes,
        )
        return artifact.id

    def _build_system_prompt(self) -> str:
        """Concatenate the three prompt files with section separators."""
        return (
            self._prompts["system"]
            + "\n\n---\n\n"
            + self._prompts["composition_bias"]
            + "\n\n---\n\n"
            + self._prompts["novel_python_flagging"]
        )

    def _build_user_prompt(
        self,
        prompt: str,
        hits: list[SearchHit],
        context: dict[str, Any] | None,
    ) -> str:
        """Compose the user-facing message: library candidates +
        user task + optional context.
        """
        candidates_block = _render_candidates(hits) if hits else (
            "(no matching library components — you may need to emit "
            "novel Python; see the novel_python_flagging rules above)"
        )
        parts = [
            "## Available library components",
            "",
            candidates_block,
            "",
            "## User task",
            "",
            prompt.strip(),
        ]
        # Strip internal plumbing keys before rendering — the LLM doesn't
        # need (or want) the run_id, and YAML's safe_dump can't serialize
        # a UUID anyway. Anything in ``_INTERNAL_CONTEXT_KEYS`` is
        # composer plumbing, not LLM-visible context.
        if context:
            llm_visible = {
                k: v for k, v in context.items()
                if k not in _INTERNAL_CONTEXT_KEYS
            }
            if llm_visible:
                parts.append("")
                parts.append("## Additional context")
                parts.append("")
                parts.append(yaml.safe_dump(
                    llm_visible, sort_keys=True, default_flow_style=False,
                ).strip())
        return "\n".join(parts)

    @classmethod
    def _load_prompts(cls, prompt_dir: Path) -> dict[str, str]:
        if not prompt_dir.is_dir():
            raise ComposerConfigurationError(
                f"prompt_dir {prompt_dir} is not a directory"
            )
        prompts: dict[str, str] = {}
        missing: list[str] = []
        for filename in REQUIRED_PROMPT_FILES:
            p = prompt_dir / filename
            if not p.is_file():
                missing.append(filename)
                continue
            prompts[p.stem] = p.read_text(encoding="utf-8")
        if missing:
            raise ComposerConfigurationError(
                f"prompt_dir {prompt_dir} missing required prompt files: "
                f"{sorted(missing)}"
            )
        return prompts


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _default_llm_factory(**kwargs: Any):
    """Default LLM client builder — imports lazily so tests that monkeypatch
    ``apecx_db_integration.agent._build_chat_llm`` can intercept.
    """
    from apecx_db_integration.agent import _build_chat_llm
    return _build_chat_llm(**kwargs)


# Matches a fenced block whose label is captured as group 1 and whose
# body is group 2. Handles both ``` and ~~~ fences per CommonMark
# (limited to ``` to keep the regex simple). Greedy on the body with
# a non-greedy-ish trailing fence.
_FENCE_RE = re.compile(
    r"```\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\n"
    r"(.*?)"
    r"\n```",
    re.DOTALL,
)


def _parse_response(content: str) -> tuple[str, dict[str, str]]:
    """Extract the single ``yaml`` fenced block and the optional
    ``novel_python`` fenced block from the LLM response.

    Returns ``(yaml_text, novel_python_dict)``. Raises
    ``ComposerResponseError`` if the yaml block is missing.

    Strictness: we accept the LLM emitting prose outside fences
    (retryable) but reject the absence of ANY yaml fence entirely
    (unparseable). Multiple yaml fences → first one wins; we log the
    extras but don't error — LLMs sometimes emit a second yaml as a
    "preview" and we'd rather take the first than fail.
    """
    blocks: dict[str, list[str]] = {}
    for match in _FENCE_RE.finditer(content):
        label = match.group(1).lower()
        body = match.group(2)
        blocks.setdefault(label, []).append(body)

    yaml_blocks = blocks.get("yaml", [])
    if not yaml_blocks:
        raise ComposerResponseError(
            "LLM response has no ```yaml fenced block. First 500 chars: "
            f"{content[:500]!r}"
        )
    if len(yaml_blocks) > 1:
        log.warning(
            "Composer response has %d yaml blocks; using the first",
            len(yaml_blocks),
        )
    yaml_text = yaml_blocks[0]

    novel_python_raw = blocks.get("novel_python", [])
    novel_python: dict[str, str] = {}
    if novel_python_raw:
        # novel_python is itself a YAML mapping step_id -> source.
        try:
            parsed = yaml.safe_load(novel_python_raw[0])
        except yaml.YAMLError as exc:
            raise ComposerResponseError(
                f"LLM response's novel_python block failed to parse: {exc}"
            ) from exc
        if parsed is None:
            parsed = {}
        if not isinstance(parsed, dict):
            raise ComposerResponseError(
                "LLM response's novel_python block must be a mapping "
                f"<step_id>: <source>; got {type(parsed).__name__}"
            )
        for k, v in parsed.items():
            if not isinstance(v, str):
                raise ComposerResponseError(
                    f"novel_python[{k!r}] must be a source string; got "
                    f"{type(v).__name__}"
                )
            novel_python[str(k)] = v

    return yaml_text, novel_python


def _render_candidates(hits: list[SearchHit]) -> str:
    """Render retrieval hits as a compact, LLM-consumable block."""
    lines: list[str] = []
    for hit in hits:
        c = hit.component
        lines.append(f"- id: {c.id}")
        lines.append(f"  name: {c.name}")
        lines.append(f"  class: {c.class_path}")
        if c.yaml_path:
            lines.append(f"  yaml: {c.yaml_path}")
        lines.append(f"  description: {c.description}")
        if c.examples:
            lines.append(f"  examples: {list(c.examples)}")
        lines.append("")
    return "\n".join(lines).rstrip()


__all__ = [
    "Composer",
    "ComposerConfigurationError",
    "ComposerResponseError",
    "REQUIRED_PROMPT_FILES",
]
