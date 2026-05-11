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
import os
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from apecx_integration.composition.component_catalog import (
    CatalogComponent,
    ComponentCatalog,
    SearchHit,
)
from apecx_integration.composition.composer_schemas import (
    ComposedWorkflow,
    ComposerConfig,
    CompositionSummary,
)
from apecx_integration.composition.differ import (
    StepCategory,
    categorize_workflow,
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


# Error classes live in ``_errors.py`` to break the circular import
# between this module and ``workflow_validator.py``; they are
# re-exported here for backward compatibility with existing callers
# that import them from this module.
from apecx_integration.composition._errors import (  # noqa: E402
    ComposerConfigurationError,
    ComposerResponseError,
)


class _RetryableValidationError(Exception):
    """Internal wrapper around ``WorkflowValidationError`` for the
    compose-validate-retry loop.

    Carrying the underlying error as an attribute (instead of
    inheriting from it) keeps the public exception hierarchy stable:
    callers that catch ``WorkflowValidationError`` still see it after
    the retry loop unwraps. The wrapper exists so the loop's
    "should I retry?" branch is a single ``except`` clause that
    doesn't accidentally swallow other ``WorkflowValidationError``s
    raised elsewhere in the call stack.
    """

    def __init__(self, workflow_validation_error) -> None:  # type: ignore[no-untyped-def]
        self.workflow_validation_error = workflow_validation_error
        super().__init__(str(workflow_validation_error))


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
        self._catalog = ComponentCatalog.from_manifests(list(config.component_catalog_paths))
        self._llm_factory = llm_factory or _default_llm_factory
        self._artifact_store = artifact_store

        # Phase-4 RAG swap-in. Loaded lazily: when ``rag_index_dir`` is
        # set we deserialize the pre-built FAISS index; when it's None
        # we keep the Phase-2 linear-scan catalog as the retrieval
        # backend. The composer never builds the index on its own —
        # the operator runs ``scripts/build_rag_index.py`` out-of-band.
        self._rag_index = None
        if config.rag_index_dir is not None:
            try:
                from nanobrain.lightweight.component_index import (
                    ComponentIndex,
                )
            except ImportError as exc:
                raise ComposerConfigurationError(
                    f"rag_index_dir is set in composer config but "
                    f"nanobrain.lightweight.component_index is not "
                    f"importable: {exc}"
                ) from exc
            index_dir = Path(config.rag_index_dir)
            if (
                not (index_dir / "faiss.bin").is_file()
                or not (index_dir / "metadata.json").is_file()
            ):
                raise ComposerConfigurationError(
                    f"rag_index_dir={index_dir} is missing faiss.bin "
                    "or metadata.json. Run scripts/build_rag_index.py "
                    "to create the index."
                )
            self._rag_index = ComponentIndex.load(index_dir)

        # Cache the import whitelist at init so that high-QPS compose()
        # calls don't re-read the file from disk on every novel-Python
        # request (audit §1.4). The whitelist is static config; reload
        # would require restarting the composer anyway.
        self._whitelist: set[str] | None = None
        if config.sandbox_whitelist_path is not None:
            self._whitelist = load_whitelist(config.sandbox_whitelist_path)

        log.info(
            "Composer initialized (Phase %s): library=%s llm=%s prompts=%d "
            "components=%d retrieval=%s persist=%s",
            "4" if self._rag_index is not None else "3" if artifact_store is not None else "2",
            config.library_version,
            config.llm_model,
            len(self._prompts),
            len(self._catalog),
            "rag" if self._rag_index is not None else "linear",
            artifact_store is not None,
        )

    @classmethod
    def from_config(cls, config_path: str | Path) -> Composer:
        """Load a ``ComposerConfig`` from YAML and build the composer."""
        path = Path(config_path)
        if not path.is_file():
            raise ComposerConfigurationError(f"composer config not found at {path}")
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ComposerConfigurationError(
                f"composer config at {path} must be a YAML mapping, got {type(raw).__name__}"
            )

        # Resolve prompt_dir relative to the config file if it's not absolute.
        prompt_dir_raw = raw.get("prompt_dir")
        if prompt_dir_raw is None:
            raise ComposerConfigurationError("composer config missing required 'prompt_dir'")
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

        rag_dir_raw = raw.get("rag_index_dir")
        if rag_dir_raw is not None:
            rdir = Path(rag_dir_raw)
            if not rdir.is_absolute():
                rdir = (path.parent / rdir).resolve()
            raw["rag_index_dir"] = rdir

        # APECX_LLM_* env vars override the YAML values so operators
        # can re-target a deploy without editing the config file. The
        # composer_config.yml's own header says exactly this — the
        # env-var honoring is what makes that promise true. Audit
        # trails (``result.llm_model``) then reflect what was actually
        # USED, not what was configured on disk.
        _apply_llm_env_overrides(raw)

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

    def _suggest_for_violation(self, source: str, *, k: int = 3) -> tuple[str, ...]:
        """Pick ``k`` components from the active retrieval backend that
        best match the rejected novel-Python source.

        Strategy: run the source text through ``_retrieve`` — the
        surrounding comments + variable names + function names carry
        the author's *intent* better than the raw violated module name
        would. ``entity_extraction`` won't match ``import subprocess``
        if you only query the import; it does match when the source
        uses words like "extract" / "entities" / "pathogens".

        Returns an empty tuple when retrieval has nothing — don't
        render a "closest matches: none" block; just stay quiet.
        """
        try:
            hits = self._retrieve(source, k=k)
        except Exception as exc:
            # Retrieval backend failing shouldn't swallow a
            # ScanViolation — the user still needs to see the real
            # sandbox error. Fall through to no suggestions, but log
            # the underlying retrieval failure so operators have a
            # signal that retrieval is broken (was a silent hole per
            # audit §1.1).
            log.warning(
                "Suggestion retrieval failed (%s: %s); "
                "ScanViolation will surface without suggestions.",
                type(exc).__name__,
                exc,
            )
            return ()
        out: list[str] = []
        for h in hits:
            c = h.component
            line = f"{c.id} — {c.description}"
            if c.yaml_path:
                line += f" (config: {c.yaml_path})"
            out.append(line)
        return tuple(out)

    def _retrieve(self, prompt: str, k: int) -> list[SearchHit]:
        """Route retrieval to RAG or linear-scan.

        When a RAG index is loaded, query FAISS and adapt each
        ``ComponentMatch`` to the ``SearchHit(CatalogComponent, score)``
        shape the composer's prompt-rendering already expects. Score
        becomes ``int(similarity * 1000)`` so the higher-is-better
        convention from linear-scan is preserved — currently unused
        by ``_render_candidates`` but kept so downstream consumers
        (e.g. diff UX in T06) can read a consistent field.
        """
        if self._rag_index is None:
            return self._catalog.search(prompt, k=k)
        matches = self._rag_index.search(prompt, k=k)
        return [
            SearchHit(
                component=CatalogComponent(
                    id=m.id,
                    name=m.name,
                    description=m.description,
                    class_path=m.class_path,
                    yaml_path=m.yaml_path,
                    examples=m.examples,
                ),
                score=int(round(m.similarity * 1000)),
            )
            for m in matches
        ]

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
                f"compose() requires a non-empty prompt string; got {type(prompt).__name__}"
            )

        # 1. Retrieve candidate components (RAG if configured, else linear-scan)
        hits = self._retrieve(prompt, k=self._config.retrieval_k)
        log.info(
            "Composer retrieval (%s): %d hits for prompt",
            "rag" if self._rag_index is not None else "linear",
            len(hits),
        )

        # 2. Build LLM messages
        system_prompt = self._build_system_prompt()
        user_prompt = self._build_user_prompt(prompt, hits, context)

        # Precompute the catalog map — used by the validator and the
        # differ (below) on every attempt. Catalog doesn't change
        # between retries.
        retrieved_class_paths = {h.component.class_path for h in hits}
        yaml_paths = {
            h.component.class_path: h.component.yaml_path for h in hits if h.component.yaml_path
        }

        # 3-6b. LLM call + parse + scanner + validate. Wrapped in a
        # compose-validate-retry loop (C1): on WorkflowValidationError,
        # re-prompt the LLM with the prior YAML + structured violations
        # so the next attempt has a precise correction signal.
        # ``max_validation_retries`` caps the budget (default 1). Other
        # error classes (ScanViolation, ComposerResponseError variants
        # like empty content or unparseable YAML) bypass the retry —
        # they are not the failure shape this loop is designed to
        # repair.
        from langchain_core.messages import (  # noqa: PLC0415
            AIMessage,
            HumanMessage,
            SystemMessage,
        )

        llm = self._llm_factory(
            temperature=self._config.temperature,
            max_tokens=self._config.max_tokens,
            model=self._config.llm_model,
            base_url=self._config.llm_base_url,
        )

        messages: list[Any] = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ]
        compose_retries = 0
        max_retries = self._config.max_validation_retries

        while True:
            yaml_text, novel_python, workflow_dict = self._invoke_and_parse(
                llm,
                messages,
            )
            try:
                self._validate_or_raise(workflow_dict, yaml_text, yaml_paths)
                break
            except _RetryableValidationError as exc:
                if compose_retries >= max_retries:
                    # Budget exhausted — raise the structured error so
                    # the caller (control plane route) marks the run
                    # FAILED with the violation payload visible.
                    raise exc.workflow_validation_error from exc
                compose_retries += 1
                log.warning(
                    "Composer validation failed on attempt %d/%d: %d "
                    "violation(s); retrying with structured feedback. "
                    "rule_ids=%s",
                    compose_retries,
                    max_retries + 1,
                    len(exc.workflow_validation_error.violations),
                    [v.rule_id for v in exc.workflow_validation_error.violations],
                )
                # Append the prior (invalid) YAML as the assistant turn
                # plus the structured-feedback payload as the next
                # user turn. The LLM sees its own response and a
                # precise diff list, not a vague "try again."
                messages.append(AIMessage(content=yaml_text))
                messages.append(
                    HumanMessage(
                        content=exc.workflow_validation_error.to_feedback_payload(),
                    )
                )

        # 7. Assemble ComposedWorkflow.
        yaml_bytes = yaml_text.encode("utf-8")

        # T06 categorization (AP §5.6). The differ runs over the parsed
        # workflow + novel_python + the retrieved catalog class paths
        # + canonical wrapper-YAML map; each step gets one row in
        # ``step_categorizations``. The resulting summary_sentence is
        # what the MCP UI / diff endpoint surfaces to the reviewer.
        categorized = categorize_workflow(
            workflow_dict=workflow_dict,
            novel_python=novel_python,
            retrieved_class_paths=retrieved_class_paths,
            catalog_yaml_paths=yaml_paths,
        )

        # Backward-compat counts: steps_reused = composed_*; the
        # original Phase-1 contract was "count of library components
        # reused" which matches composed_standard + composed_parameterized
        # + composed_wrapped.
        steps_reused = (
            categorized.count(StepCategory.COMPOSED_STANDARD)
            + categorized.count(StepCategory.COMPOSED_PARAMETERIZED)
            + categorized.count(StepCategory.COMPOSED_WRAPPED)
        )
        summary = CompositionSummary(
            steps_reused=steps_reused,
            steps_generated=len(novel_python),
            steps_swapped=0,
            summary_sentence=categorized.summary_sentence,
            review_notes=tuple(f"novel Python step: {k}" for k in novel_python),
            step_categorizations=categorized.categorizations,
            compose_retries=compose_retries,
        )
        llm_model_version_hash = hashlib.sha256(self._config.llm_model.encode("utf-8")).hexdigest()

        # 8. Persist via ArtifactStore when both an injected store AND a
        # run_id context are available. Otherwise stay on the Phase-2
        # in-memory path (synthesize uuid4, no DB write). This split
        # preserves compatibility for tests that don't want DB setup.
        artifact_id = self._persist_or_synthesize(
            yaml_bytes=yaml_bytes,
            prompt=prompt,
            context=context,
            composition_summary=summary,
            novel_python=novel_python,
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

    def _invoke_and_parse(
        self,
        llm: Any,
        messages: list[Any],
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        """One LLM round-trip: invoke → parse → safe_load.

        Raises:
            ComposerResponseError: empty content, missing yaml fence,
                YAML syntax error, or non-mapping top level.
            ScanViolation: novel-Python step violated the import
                whitelist (T13).

        Returns:
            ``(yaml_text, novel_python, workflow_dict)`` — the parsed
            workflow ready for framework validation.
        """
        response = llm.invoke(messages)
        raw_content = getattr(response, "content", str(response))
        if not isinstance(raw_content, str) or not raw_content.strip():
            raise ComposerResponseError(
                "LLM response content was empty or non-string "
                f"(got {type(raw_content).__name__}={raw_content!r}). "
                "The composer expected a yaml-fenced block; nothing "
                "to parse."
            )

        yaml_text, novel_python = _parse_response(raw_content)

        # T13 scanner over novel Python (if any). On violation,
        # enrich the exception with "closest matches in component
        # library" suggestions so the message steers toward
        # composition instead of fighting the whitelist.
        if novel_python and self._whitelist is not None:
            scanner = ImportScanner(whitelist=self._whitelist)
            for _step_id, source in novel_python.items():
                result = scanner.scan(source)
                if not result.ok:
                    suggestions = self._suggest_for_violation(source)
                    raise ScanViolation(result, suggestions=suggestions)

        try:
            workflow_dict = yaml.safe_load(yaml_text)
        except yaml.YAMLError as exc:
            raise ComposerResponseError(
                f"LLM response's yaml block failed to parse: {exc}"
            ) from exc
        if not isinstance(workflow_dict, dict):
            raise ComposerResponseError(
                f"LLM response's yaml block must be a mapping at top "
                f"level, got {type(workflow_dict).__name__}"
            )
        return yaml_text, novel_python, workflow_dict

    def _validate_or_raise(
        self,
        workflow_dict: dict[str, Any],
        yaml_text: str,
        yaml_paths: dict[str, str],
    ) -> None:
        """Run framework-rule validation on a parsed workflow.

        Raises ``_RetryableValidationError`` wrapping the underlying
        ``WorkflowValidationError`` when the workflow is framework-
        illegal. The wrapping is internal — callers that catch the
        underlying error continue to work because the wrapper carries
        it as an attribute, and the compose() retry loop unwraps and
        re-raises.
        """
        from apecx_integration.composition.workflow_validator import (
            WorkflowValidationError,
            validate_workflow_against_framework,
        )

        violations = validate_workflow_against_framework(
            workflow_dict,
            catalog_yaml_paths=yaml_paths,
        )
        if violations:
            err = WorkflowValidationError(
                violations=violations,
                yaml_text=yaml_text,
            )
            raise _RetryableValidationError(err)

    def _persist_or_synthesize(
        self,
        *,
        yaml_bytes: bytes,
        prompt: str,
        context: dict[str, Any] | None,
        composition_summary: CompositionSummary,
        novel_python: dict[str, str],
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
                # T06: persist per-step categorization so
                # /workflows/diff can surface it without re-running
                # retrieval, and the novel_python source so
                # /workflows/novel_python has something to return.
                "step_categorizations": [
                    {
                        "step_id": s.step_id,
                        "step_class": s.step_class,
                        "category": s.category.value,
                        "reason": s.reason,
                        "retrieval_gap": s.retrieval_gap,
                    }
                    for s in composition_summary.step_categorizations
                ],
                "review_notes": list(composition_summary.review_notes),
                "novel_python_by_step": dict(novel_python),
                # C1 (2026-05-11): how many compose-validate-retry
                # rounds were needed. Operators / regression-tracking
                # queries SELECT this to measure LLM prompt drift over
                # time.
                "compose_retries": composition_summary.compose_retries,
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
        candidates_block = (
            _render_candidates(hits)
            if hits
            else (
                "(no matching library components — you may need to emit "
                "novel Python; see the novel_python_flagging rules above)"
            )
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
            llm_visible = {k: v for k, v in context.items() if k not in _INTERNAL_CONTEXT_KEYS}
            if llm_visible:
                parts.append("")
                parts.append("## Additional context")
                parts.append("")
                parts.append(
                    yaml.safe_dump(
                        llm_visible,
                        sort_keys=True,
                        default_flow_style=False,
                    ).strip()
                )
        return "\n".join(parts)

    @classmethod
    def _load_prompts(cls, prompt_dir: Path) -> dict[str, str]:
        if not prompt_dir.is_dir():
            raise ComposerConfigurationError(f"prompt_dir {prompt_dir} is not a directory")
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
                f"prompt_dir {prompt_dir} missing required prompt files: {sorted(missing)}"
            )
        return prompts


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _default_llm_factory(**kwargs: Any):
    """Default LLM client builder — imports lazily so a missing
    optional dependency (langchain_openai) does not break composer
    import for callers that inject their own ``llm_factory``.

    Tests that need a placeholder LLM should pass ``llm_factory=...``
    to the ``Composer`` constructor — that is the supported seam
    (see ``tests/integration/test_composer_phase2.py``). Direct
    monkeypatching of this factory is not supported.

    Implementation lives in ``apecx_integration.agents._llm_factory``.
    The factory honors APECX_LLM_BASE_URL / APECX_LLM_MODEL /
    APECX_LLM_API_KEY / APECX_LLM_TEMPERATURE / APECX_LLM_MAX_TOKENS
    env vars; local LLMs (Ollama, vLLM) are first-class — both expose
    an OpenAI-compatible chat-completions endpoint.
    """
    from apecx_integration.agents._llm_factory import build_chat_llm

    return build_chat_llm(**kwargs)


# Matches a fenced block whose label is captured as group 1 and whose
# body is group 2. Handles both ``` and ~~~ fences per CommonMark
# (limited to ``` to keep the regex simple). Greedy on the body with
# a non-greedy-ish trailing fence.
#
# Audit §1.3: `\n\s*` before the closing fence (instead of plain
# `\n```) tolerates trailing whitespace or a blank line between the
# body and the closing fence — valid CommonMark, occasionally
# emitted by LLMs whose training distribution includes that pattern.
# Pre-fix the parser silently failed to match such blocks and the
# composer raised "no ```yaml fenced block" with no hint that the
# block existed but had a trailing blank line.
_FENCE_RE = re.compile(
    r"```\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\n"
    r"(.*?)"
    r"\n\s*```",
    re.DOTALL,
)


def _apply_llm_env_overrides(raw: dict[str, Any]) -> None:
    """Honor the ``APECX_LLM_*`` env-var contract the composer_config
    header promises. In-place edit of the raw mapping before pydantic
    validation runs.

    Mapping (env → config key):
        APECX_LLM_MODEL       → llm_model
        APECX_LLM_BASE_URL    → llm_base_url
        APECX_LLM_TEMPERATURE → temperature (float)
        APECX_LLM_MAX_TOKENS  → max_tokens  (int)

    Unset env vars leave the YAML value untouched. Invalid numeric
    values raise ValueError at pydantic validation; the composer
    surfaces that as ``ComposerConfigurationError``.
    """
    str_pairs = (
        ("APECX_LLM_MODEL", "llm_model"),
        ("APECX_LLM_BASE_URL", "llm_base_url"),
    )
    for env, key in str_pairs:
        value = os.environ.get(env)
        if value:
            raw[key] = value

    numeric_pairs = (
        ("APECX_LLM_TEMPERATURE", "temperature", float),
        ("APECX_LLM_MAX_TOKENS", "max_tokens", int),
    )
    for env, key, caster in numeric_pairs:
        value = os.environ.get(env)
        if value is not None and value != "":
            raw[key] = caster(value)


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
            f"LLM response has no ```yaml fenced block. First 500 chars: {content[:500]!r}"
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
                    f"novel_python[{k!r}] must be a source string; got {type(v).__name__}"
                )
            novel_python[str(k)] = v

    return yaml_text, novel_python


def _render_candidates(hits: list[SearchHit]) -> str:
    """Render retrieval hits as a compact, LLM-consumable block.

    B1 (2026-05-11): each candidate with a wrapper YAML now carries
    an ``emit_step`` block that shows the LLM exactly what to paste
    into ``steps:`` for that component. The block is YAML-formatted
    and uses the canonical class path + canonical config path, so the
    "what should I literally write?" answer is two lines below the
    description. This addresses the recurring drift pattern where
    the LLM saw ``yaml: steps/foo.yml`` but still synthesized an
    inline dict because it had to assemble the step shape itself
    from prose rules.
    """
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
        if c.yaml_path:
            # Ready-to-paste step shape using a short, semantic
            # step_id derived from the component name. The LLM is
            # expected to swap the step_id for a task-appropriate
            # one — the literal strings to copy are the class path
            # and the config path.
            stub_id = c.name.lower().replace(" ", "_").replace("-", "_")
            lines.append("  emit_step: |")
            lines.append(f"    {stub_id}:")
            lines.append(f"      class: {c.class_path}")
            lines.append(f'      config: "{c.yaml_path}"')
        lines.append("")
    return "\n".join(lines).rstrip()


__all__ = [
    "Composer",
    "ComposerConfigurationError",
    "ComposerResponseError",
    "REQUIRED_PROMPT_FILES",
]
