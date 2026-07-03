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
    PromptBudget,
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


# G78 (2026-05-16): module-level helpers extracted to focused sibling
# modules. Re-exported here so existing test imports
# (``from apecx_integration.composition.composer import _xxx``) keep
# working without change. The noqa is required because composer.py
# patches ``sys.path`` near the top of the file BEFORE these imports
# run.
from apecx_integration.composition._composer_candidates import (  # noqa: E402
    _render_candidates,
    _render_candidates_spec,
)
from apecx_integration.composition._composer_llm_factory import (  # noqa: E402
    _apply_llm_env_overrides,
    _default_llm_factory,
)
from apecx_integration.composition._composer_parsing import (  # noqa: E402
    _format_class_not_found_feedback,
    _format_parse_feedback,
    _is_class_not_found_error,
    _is_repairable_parse_error,
    _parse_response,
)
from apecx_integration.composition._composer_rerank import (  # noqa: E402
    _rerank_by_class_name_match,
)
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
        self._prompt_budgets: dict[str, PromptBudget] = self._build_prompt_budgets(
            self._prompts,
            soft_cap_kb=config.prompt_soft_cap_kb,
            hard_cap_kb=config.prompt_hard_cap_kb,
        )
        self._enforce_prompt_budgets(self._prompt_budgets)
        self._catalog = ComponentCatalog.from_manifests(list(config.component_catalog_paths))
        self._llm_factory = llm_factory or _default_llm_factory
        self._artifact_store = artifact_store

        # SKEL (2026-05-12): load the skeleton library so spec-mode
        # callers can emit ``{"skeleton": "name"}`` instead of
        # assembling a full spec. Skeletons live alongside the
        # composer module under ``composition/skeletons/*.yml``.
        # An empty library is a valid state — the composer simply
        # doesn't advertise any skeletons in its prompt.
        from apecx_integration.composition.skeletons import (  # noqa: PLC0415
            SkeletonLibrary,
        )

        skeletons_dir = Path(__file__).parent / "skeletons"
        self._skeleton_library = SkeletonLibrary.from_dir(skeletons_dir)

        # REVIEW-AGENT (2026-05-12): build the semantic reviewer when
        # enabled. The reviewer uses the SAME llm_factory the composer
        # uses, so operators only configure one LLM provider. Disabled
        # by default — flip on via APECX_COMPOSER_REVIEW=1 when
        # adoption / quality is at stake; redundant on the trusted-
        # skeleton path.
        self._reviewer = None
        if config.enable_review:
            from apecx_integration.composition.reviewer import (  # noqa: PLC0415
                WorkflowReviewer,
            )

            self._reviewer = WorkflowReviewer.from_prompt_dir(
                config.prompt_dir,
                llm_factory=self._llm_factory,
                model=config.llm_model,
                base_url=config.llm_base_url,
                temperature=config.temperature,
                max_tokens=min(1024, config.max_tokens),
            )
            if self._reviewer is None:
                raise ComposerConfigurationError(
                    "enable_review=True but reviewer_system.md not "
                    "found in prompt_dir. Ship the file or set "
                    "enable_review=False (APECX_COMPOSER_REVIEW=0)."
                )

        # Phase-4 RAG swap-in. Loaded lazily: when ``rag_index_dir`` is
        # set we deserialize the pre-built FAISS index; when it's None
        # we keep the Phase-2 linear-scan catalog as the retrieval
        # backend. The composer never builds the index on its own —
        # the operator runs ``scripts/build_rag_index.py`` out-of-band.
        # Semantic (FAISS) retrieval is an OPT-IN enhancement: set
        # ``rag_index_dir`` and build the index out-of-band
        # (``scripts/build_rag_index.py``). When it is unset, absent,
        # un-importable, or STALE versus the current component corpus, the
        # composer DEGRADES LOUD to the Phase-2 linear-scan catalog rather than
        # refusing to start or — worse — silently retrieving over an outdated
        # corpus. Linear-scan is a correct (if lower-recall) fallback, so a
        # broken/stale index must never break composition; the warning surfaces
        # the condition + the fix. The composer never builds the index itself.
        self._rag_index = None
        if config.rag_index_dir is not None:
            self._rag_index = self._load_rag_index_or_degrade(config)

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

    def _load_rag_index_or_degrade(self, config: ComposerConfig):
        """Load the FAISS ``ComponentIndex`` at ``config.rag_index_dir``, or
        return ``None`` (degrade to linear-scan) with a LOUD warning when it is
        un-importable, missing, corrupt, or STALE versus the current corpus.

        Never raises — a broken/stale semantic index must not break composition.
        """
        try:
            from nanobrain.lightweight.component_index import (  # noqa: PLC0415
                ComponentIndex,
            )
        except ImportError as exc:
            log.warning(
                "rag_index_dir is set but nanobrain.lightweight.component_index "
                "is not importable (%s); using linear-scan retrieval. Install the "
                "'rag' extra to enable semantic retrieval.",
                exc,
            )
            return None

        index_dir = Path(config.rag_index_dir)
        if not (index_dir / "faiss.bin").is_file() or not (index_dir / "metadata.json").is_file():
            log.warning(
                "rag_index_dir=%s is missing faiss.bin/metadata.json; using "
                "linear-scan retrieval. Run scripts/build_rag_index.py to enable "
                "semantic retrieval.",
                index_dir,
            )
            return None

        try:
            candidate = ComponentIndex.load(index_dir)
        except Exception as exc:  # noqa: BLE001 — a corrupt index must degrade, not crash
            log.warning(
                "rag_index at %s failed to load (%s); using linear-scan "
                "retrieval. Rebuild with scripts/build_rag_index.py.",
                index_dir,
                exc,
            )
            return None

        try:
            stale = candidate.is_stale(
                list(config.component_catalog_paths),
                library_version=config.library_version,
            )
        except Exception as exc:  # noqa: BLE001 — can't verify freshness => don't trust it
            # The staleness check re-reads the manifests; if that fails we cannot
            # confirm the index matches the corpus, so degrade rather than risk
            # serving a possibly-stale index. Keeps the method's never-raises
            # contract locally true (not reliant on from_manifests pre-validation).
            log.warning(
                "rag_index at %s: staleness check failed (%s); using linear-scan "
                "retrieval to avoid serving a possibly-stale index.",
                index_dir,
                exc,
            )
            return None
        if stale:
            log.warning(
                "rag_index at %s is STALE vs the current component corpus (built "
                "from an older manifest set / library_version); using linear-scan "
                "retrieval. Rebuild with scripts/build_rag_index.py to re-enable "
                "semantic retrieval over the current corpus.",
                index_dir,
            )
            return None

        log.info(
            "rag_index at %s loaded (fresh vs corpus); semantic retrieval ON.",
            index_dir,
        )
        return candidate

    def llm_for_role(self, role: str, **overrides: Any) -> Any:
        """Build a chat LLM bound to the model assigned to ``role``.

        BENCH-P0 (2026-05-12). When ``role`` is registered in
        ``config.model_roles`` (either via YAML or via the
        ``APECX_LLM_MODEL_<ROLE>`` env override), the returned LLM
        uses that role's ``(model, base_url)``. Otherwise it falls
        back to the composer's default single-model binding
        (``config.llm_model`` / ``config.llm_base_url``). This
        preserves T01 AC1 strict-path behavior: an existing config
        without ``model_roles`` is unaffected.

        ``overrides`` are forwarded to the LLM factory verbatim —
        callers typically pass ``temperature``, ``max_tokens``, etc.
        """
        role_cfg = self._config.model_roles.get(role)
        if role_cfg is not None:
            model = role_cfg.model
            base_url = role_cfg.base_url or self._config.llm_base_url
        else:
            model = self._config.llm_model
            base_url = self._config.llm_base_url
        kwargs: dict[str, Any] = {
            "temperature": self._config.temperature,
            "max_tokens": self._config.max_tokens,
            "model": model,
            "base_url": base_url,
        }
        kwargs.update(overrides)
        return self._llm_factory(**kwargs)

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
        # B3 (2026-05-11): re-rank by class-name substring match
        # against the prompt's tokens. Cheap second pass that boosts
        # components the user named explicitly (or whose
        # ``class_name`` appears in the prompt text). The base
        # retrieval scores stay authoritative for ordering when no
        # token match exists; this only nudges named components to
        # the top so the LLM sees them inside the candidate block.
        hits = _rerank_by_class_name_match(hits, prompt)
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
            # The compose-validate-retry loop covers TWO repairable
            # failure shapes:
            #
            #   1. ``WorkflowValidationError`` — A1 caught a
            #      framework-rule violation; feed structured
            #      violations back to the LLM.
            #   2. ``ComposerResponseError`` whose message names a
            #      shape mistake the LLM can plausibly correct
            #      (top-level-not-mapping, multiple-yaml-blocks-with-
            #      a-list-first). Surfaced by the real-ollama E2E
            #      run 2026-05-11: mistral-nemo emitted three yaml
            #      blocks, the first one a bare list — the parser
            #      raised, the retry was never reached, the user
            #      saw an opaque parse failure.
            #
            # Other ComposerResponseError variants (empty content,
            # unparseable YAML) still bypass — those are not
            # repairable failure modes a retry would help with.
            try:
                yaml_text, novel_python, workflow_dict = self._invoke_and_parse(
                    llm,
                    messages,
                )
            except ComposerResponseError as exc:
                if not _is_repairable_parse_error(exc):
                    raise
                if compose_retries >= max_retries:
                    raise
                compose_retries += 1
                # A hallucinated class_name is repairable, but the generic shape-
                # correction hint is the WRONG feedback (the YAML shape was fine) — feed
                # back the VALID catalog leaf names so the LLM picks a real class instead
                # of re-inventing one and exhausting the retries.
                if _is_class_not_found_error(exc):
                    # The expander resolves a step's class_name only by the class-path LEAF
                    # (e.g. `RagSynthesisStep`) or a full dotted path — NOT the composite
                    # catalog id (`rag_e2e_synthesis/rag_synthesis:A2`). Feed the LEAF names so
                    # the LLM picks a name the expander can actually resolve.
                    feedback = _format_class_not_found_feedback(
                        exc,
                        sorted({c.class_path.rsplit(".", 1)[-1] for c in self._catalog.components}),
                    )
                else:
                    feedback = _format_parse_feedback(exc)
                log.warning(
                    "Composer parse failed on attempt %d/%d: %s; "
                    "retrying with correction feedback.",
                    compose_retries,
                    max_retries + 1,
                    exc,
                )
                # No prior YAML to thread back — the parse failed
                # before we had a clean yaml_text. Just append the
                # correction as the next user turn so the LLM sees
                # the diagnostic.
                messages.append(HumanMessage(content=feedback))
                continue

            # T13 import-whitelist scan over novel Python — HOISTED here (2026-07-01) from
            # _invoke_and_parse so it runs for BOTH composer modes (spec + monolithic); spec
            # mode used to return before the scan and shipped UNSCANNED LLM Python. A
            # ScanViolation is fail-closed: it is NOT a ComposerResponseError, so the
            # parse-retry handler above does not catch it — it propagates out of compose().
            # A whitelist violation must not be "retried" into compliance.
            if novel_python and self._whitelist is not None:
                scanner = ImportScanner(whitelist=self._whitelist)
                for _step_id, source in novel_python.items():
                    result = scanner.scan(source)
                    if not result.ok:
                        raise ScanViolation(result, suggestions=self._suggest_for_violation(source))

            # CPR (2026-05-11): catalog-grounded class-path repair.
            # Auto-corrects the dominant LLM hallucination shape
            # (leaf class name correct, module path drifted —
            # e.g. ``rag_synthesis.RagSynthesisStep`` →
            # ``rag_synthesis_step.RagSynthesisStep``). Only repairs
            # when the leaf class name has EXACTLY one match in the
            # catalog; ambiguous matches are left for the validator
            # to surface as ``step_class_unresolvable`` with a
            # "did you mean?" hint built from the same resolver.
            from apecx_integration.composition.class_path_resolver import (  # noqa: PLC0415
                repair_workflow_class_paths,
            )

            full_catalog_paths = {
                c.class_path for c in self._catalog.components
            } | retrieved_class_paths
            repairs = repair_workflow_class_paths(workflow_dict, full_catalog_paths)
            if repairs:
                # Re-encode the YAML so the persisted artifact carries
                # the corrected class paths. Otherwise the artifact
                # would silently disagree with the runtime workflow.
                yaml_text = yaml.safe_dump(workflow_dict, sort_keys=False, default_flow_style=False)
                for r in repairs:
                    log.warning(
                        "Auto-corrected class path on step %s: %s -> %s",
                        r.step_id,
                        r.emitted,
                        r.resolved,
                    )

            try:
                self._validate_or_raise(
                    workflow_dict,
                    yaml_text,
                    yaml_paths,
                    catalog_class_paths=full_catalog_paths,
                    novel_python=novel_python,
                )
                # Stash the repairs onto the workflow_dict so the
                # caller can read them when building CompositionSummary.
                workflow_dict["_apecx_class_path_repairs"] = [
                    (r.step_id, r.emitted, r.resolved) for r in repairs
                ]
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
        # CPR (2026-05-11): pop the resolver's repair record off the
        # workflow_dict and thread it into the summary. Lives on the
        # workflow_dict during the validate-loop to avoid an extra
        # parameter on _validate_or_raise.
        repairs_raw = workflow_dict.pop("_apecx_class_path_repairs", []) or []
        class_path_repairs = tuple((str(sid), str(emt), str(res)) for sid, emt, res in repairs_raw)

        # REVIEW-AGENT (2026-05-12): second-pass semantic reviewer.
        # Runs AFTER the structural validator + CPR repairs but
        # BEFORE the composer returns. The reviewer's verdict goes
        # into CompositionSummary; rejections add concerns to
        # review_notes so the human reviewer at the approval UI
        # sees them. We do NOT trigger another compose retry on
        # reject — at this point the workflow is structurally OK
        # and a retry would just re-elicit the same semantic
        # mismatch from the LLM. Better to surface the verdict +
        # let the human reviewer decide.
        review_verdict_dict: dict | None = None
        extra_review_notes: tuple[str, ...] = ()
        if self._reviewer is not None:
            verdict = await self._reviewer.review(
                user_prompt=prompt,
                yaml_text=yaml_text,
                summary_sentence=categorized.summary_sentence,
                candidates_block=_render_candidates(hits),
            )
            review_verdict_dict = {
                "approved": verdict.approved,
                "reasoning": verdict.reasoning,
                "concerns": list(verdict.concerns),
                "review_used": verdict.review_used,
            }
            log.info(
                "Composer review: approved=%s reasoning=%s concerns=%d",
                verdict.approved,
                verdict.reasoning[:160],
                len(verdict.concerns),
            )
            if not verdict.approved:
                extra_review_notes = (
                    f"reviewer rejected: {verdict.reasoning}",
                    *(f"reviewer concern: {c}" for c in verdict.concerns),
                )

        from apecx_integration.composition.env_manifest import build_env_manifest  # noqa: PLC0415

        summary = CompositionSummary(
            steps_reused=steps_reused,
            steps_generated=len(novel_python),
            steps_swapped=0,
            summary_sentence=categorized.summary_sentence,
            review_notes=tuple(f"novel Python step: {k}" for k in novel_python)
            + extra_review_notes,
            step_categorizations=categorized.categorizations,
            compose_retries=compose_retries,
            class_path_repairs=class_path_repairs,
            review_verdict=review_verdict_dict,
            env_manifest=build_env_manifest(
                llm_model=self._config.llm_model,
                llm_base_url=getattr(self._config, "llm_base_url", None),
            ),
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

        B2 (2026-05-11): wraps the actual ``llm.invoke`` call in a
        try/except so a provider 5xx surfaces with full diagnostic
        context (model, message lengths, exception type + body)
        instead of an opaque traceback. The provider's HTTP response
        body — when LangChain attaches one — is logged AS-IS so
        operators have everything they need to file a backend bug.
        The exception re-raises unchanged so callers see the original
        failure class.
        """
        import time

        start = time.monotonic()
        total_chars = sum(len(getattr(m, "content", "")) for m in messages)
        try:
            response = llm.invoke(messages)
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            self._log_llm_failure(
                exc=exc,
                elapsed_ms=elapsed_ms,
                message_count=len(messages),
                total_chars=total_chars,
            )
            raise
        elapsed_ms = int((time.monotonic() - start) * 1000)
        raw_content = getattr(response, "content", str(response))
        log.debug(
            "Composer LLM call: model=%s msgs=%d in_chars=%d out_chars=%d elapsed_ms=%d",
            self._config.llm_model,
            len(messages),
            total_chars,
            len(raw_content) if isinstance(raw_content, str) else -1,
            elapsed_ms,
        )
        if not isinstance(raw_content, str) or not raw_content.strip():
            raise ComposerResponseError(
                "LLM response content was empty or non-string "
                f"(got {type(raw_content).__name__}={raw_content!r}). "
                "The composer expected a yaml-fenced block; nothing "
                "to parse."
            )

        # SPEC2 (2026-05-11): in spec mode, the LLM emits a JSON
        # MinimalWorkflowSpec; parse it + expand to workflow_dict via
        # the deterministic template expander. The rest of the
        # pipeline (CPR + A1 + C1) is unchanged.
        if self._config.composer_mode == "spec":
            return self._parse_spec_response(raw_content)

        yaml_text, novel_python = _parse_response(raw_content)

        # NOTE (2026-07-01): the T13 import-whitelist scan over novel Python was HOISTED to
        # compose() — the single post-parse chokepoint BOTH composer modes funnel through — so
        # spec mode is scanned too. It used to live here, on the monolithic-only branch, and
        # spec mode returned (in _invoke_and_parse) before reaching it, shipping unscanned Python.
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

    def _parse_spec_response(self, raw_content: str) -> tuple[str, dict[str, str], dict[str, Any]]:
        """SPEC2: parse a JSON MinimalWorkflowSpec → workflow_dict.

        Three failure modes — all raised as ``ComposerResponseError``
        so the existing C1 parse-retry path can engage:

          1. No ``json`` fenced block in the response.
          2. JSON syntax invalid.
          3. Spec doesn't match the MinimalWorkflowSpec schema
             (Pydantic raises ValidationError). The error message
             names the offending field so the retry feedback can
             point the LLM precisely.
          4. Spec is structurally valid but references an unknown
             or ambiguous class_name — SpecExpansionError surfaces.
        """
        import json
        import re as _re

        from apecx_integration.composition.workflow_spec import (  # noqa: PLC0415
            MinimalWorkflowSpec,
            SpecExpansionError,
            expand_spec,
        )

        fence = _re.search(
            r"```(?:json)?\s*\n(.*?)\n```",
            raw_content,
            flags=_re.DOTALL,
        )
        if not fence:
            raise ComposerResponseError(
                "spec mode: LLM response has no ```json fenced block. "
                "Emit exactly one fenced JSON block containing the "
                "MinimalWorkflowSpec object."
            )
        try:
            raw_spec = json.loads(fence.group(1))
        except json.JSONDecodeError as exc:
            raise ComposerResponseError(
                f"spec mode: JSON in the fenced block did not parse: {exc}"
            ) from exc

        # SKEL (2026-05-12): {"skeleton": "name"} shorthand.
        # When the LLM emits a top-level mapping with a ``skeleton``
        # key, look up the pre-authored Skeleton and use its
        # embedded spec verbatim. This is the smallest possible LLM
        # output for the N most common workflow shapes.
        if isinstance(raw_spec, dict) and "skeleton" in raw_spec:
            skel_name = str(raw_spec["skeleton"])
            skel = self._skeleton_library.get(skel_name)
            if skel is None:
                names = ", ".join(self._skeleton_library.names()) or "(none)"
                raise ComposerResponseError(
                    f"spec mode: skeleton {skel_name!r} not found in the "
                    f"library. Available: {names}. Either pick a listed "
                    "skeleton or emit a full MinimalWorkflowSpec object."
                )
            spec = skel.spec
        else:
            try:
                spec = MinimalWorkflowSpec.model_validate(raw_spec)
            except Exception as exc:
                raise ComposerResponseError(
                    f"spec mode: emitted JSON did not match MinimalWorkflowSpec: {exc}"
                ) from exc

        try:
            workflow_dict, _warnings = expand_spec(spec, list(self._catalog.components))
        except SpecExpansionError as exc:
            raise ComposerResponseError(
                f"spec mode: expander could not realize the spec: {exc}"
            ) from exc

        # Carry over the novel_python fence the spec emitted via its
        # private ``_apecx_novel_python_by_step`` key so the existing
        # downstream code (sandbox scanner, generated_metadata) sees
        # the same shape as the monolithic path.
        novel_python: dict[str, str] = workflow_dict.pop("_apecx_novel_python_by_step", None) or {}

        # Re-encode as YAML for downstream artifact persistence +
        # validator feedback (it expects a yaml_text companion).
        yaml_text = yaml.safe_dump(workflow_dict, sort_keys=False, default_flow_style=False)
        return yaml_text, novel_python, workflow_dict

    def _log_llm_failure(
        self,
        *,
        exc: BaseException,
        elapsed_ms: int,
        message_count: int,
        total_chars: int,
    ) -> None:
        """Surface full LLM-call diagnostics on failure (B2).

        Captures:
          - exception type + message
          - any HTTP response body LangChain attached (response /
            body / json attrs are common shapes across SDKs)
          - elapsed time and message volumes
          - the actual model + base_url so a misrouted call (wrong
            endpoint, wrong model name) shows up immediately

        The intent is operator self-service: a single WARNING line
        carries everything needed to file a backend bug without
        re-running with debug logging.
        """
        provider_body = None
        for attr in ("response", "body", "json"):
            candidate = getattr(exc, attr, None)
            if candidate is not None:
                # Best-effort string-ify; provider SDKs differ wildly
                # on whether response is a requests.Response, an
                # httpx.Response, or a dict.
                try:
                    text_attr = getattr(candidate, "text", None)
                    if isinstance(text_attr, str):
                        provider_body = text_attr[:4000]
                        break
                    provider_body = str(candidate)[:4000]
                    break
                except Exception:
                    continue
        log.warning(
            "Composer LLM call FAILED: model=%s base_url=%s "
            "exc_type=%s exc_msg=%s elapsed_ms=%d msgs=%d "
            "total_in_chars=%d provider_body=%s",
            self._config.llm_model,
            self._config.llm_base_url,
            type(exc).__name__,
            str(exc)[:1000],
            elapsed_ms,
            message_count,
            total_chars,
            provider_body if provider_body else "(none captured)",
        )

    def _validate_or_raise(
        self,
        workflow_dict: dict[str, Any],
        yaml_text: str,
        yaml_paths: dict[str, str],
        *,
        catalog_class_paths: set[str] | None = None,
        novel_python: dict[str, str] | None = None,
    ) -> None:
        """Run framework-rule validation on a parsed workflow.

        Raises ``_RetryableValidationError`` wrapping the underlying
        ``WorkflowValidationError`` when the workflow is framework-
        illegal. The wrapping is internal — callers that catch the
        underlying error continue to work because the wrapper carries
        it as an attribute, and the compose() retry loop unwraps and
        re-raises.

        ``catalog_class_paths`` enables CPR's "did you mean X?" hints
        on ``step_class_unresolvable`` violations. Passing the full
        catalog (not just retrieval hits) lets the validator suggest
        components A2 would have rescued.
        """
        from apecx_integration.composition.workflow_validator import (
            WorkflowValidationError,
            WorkflowViolation,
            validate_workflow_against_framework,
        )

        violations: list[WorkflowViolation] = list(
            validate_workflow_against_framework(
                workflow_dict,
                catalog_yaml_paths=yaml_paths,
                catalog_class_paths=catalog_class_paths,
            )
        )

        # WS2b: AST-validate the LLM's novel Python BEFORE acceptance. The T13
        # import-scan (hoisted into compose(), runs before this) only checks imports; it does NOT
        # check the source parses, defines the referenced class, or obeys the
        # framework (no execute()/from_config override). Without this, a novel
        # step that imports-clean but is structurally broken passes compose and
        # fails only at workflow-run time. Folding the issues into the same
        # violation list routes them through the existing C1 retry loop, and it
        # runs for BOTH composer modes (monolithic + spec) since both reach here.
        if novel_python:
            from apecx_integration.composition.novel_python_validation import (  # noqa: PLC0415
                validate_python_structure,
            )

            steps_block = workflow_dict.get("steps") or {}
            for step_id, source in novel_python.items():
                entry = ""
                step_body = steps_block.get(step_id)
                if isinstance(step_body, dict):
                    entry = str(step_body.get("class") or "").rpartition(".")[2]
                for issue in validate_python_structure(str(source), entry):
                    violations.append(
                        WorkflowViolation(
                            rule_id="novel_python_invalid",
                            path=f"novel_python.{step_id}",
                            message=issue,
                            suggested_fix=(
                                "Fix the novel Python so it parses, defines the "
                                "referenced class at module scope, and obeys the "
                                "framework: implement `async def process`, never "
                                "override `execute`/`from_config`, and import only "
                                "from real nanobrain submodules."
                            ),
                        )
                    )

        if violations:
            err = WorkflowValidationError(
                violations=tuple(violations),
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
            composition_summary=_persisted_composition_summary(composition_summary, novel_python),
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
        """Build the system prompt for the active composer mode.

        SPEC2 (2026-05-11): when ``composer_mode == "spec"``, return
        only the distilled spec_system.md cheat sheet (~2k tokens).
        The candidate block format is also different — see
        ``_build_user_prompt``. The monolithic mode keeps its 3-file
        concatenation so the existing test surface is unchanged.
        """
        if self._config.composer_mode == "spec":
            spec_prompt = self._prompts.get("spec_system")
            if spec_prompt is None:
                raise ComposerConfigurationError(
                    "composer_mode=spec requires spec_system.md in "
                    "prompt_dir; not found. Ship the file or switch to "
                    "composer_mode=monolithic."
                )
            # SKEL (2026-05-12): append the skeleton library so the
            # LLM can pick a pre-authored shape by name. Empty
            # library = empty block; no change to the LLM contract.
            skeleton_block = self._skeleton_library.render_prompt_block()
            if skeleton_block:
                spec_prompt = (
                    spec_prompt
                    + "\n\n---\n\n"
                    + skeleton_block
                    + "\n\n"
                    + "If one of the skeletons fits the user's task "
                    "exactly, emit:\n\n"
                    "```json\n"
                    '{"skeleton": "<name>"}\n'
                    "```\n\n"
                    "Otherwise, emit the full MinimalWorkflowSpec as "
                    "described above."
                )
            return spec_prompt
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

        SPEC2 (2026-05-11): when ``composer_mode == "spec"``, the
        candidate block uses a compact "leaf class + I/O data unit
        names" format the spec_system.md prompt expects. Monolithic
        mode keeps its richer block.
        """
        if hits:
            candidates_block = (
                _render_candidates_spec(hits)
                if self._config.composer_mode == "spec"
                else _render_candidates(hits)
            )
        else:
            candidates_block = (
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
        # SPEC2 (2026-05-11): optionally load the spec-mode cheat sheet.
        # Treat as optional so older composer_config.yml files that
        # don't have spec_system.md still load. The composer raises
        # ComposerConfigurationError later if the operator sets
        # composer_mode=spec but the file is missing.
        for optional_name in ("spec_system.md",):
            p = prompt_dir / optional_name
            if p.is_file():
                prompts[p.stem] = p.read_text(encoding="utf-8")
        return prompts

    @property
    def prompt_budgets(self) -> dict[str, PromptBudget]:
        """Read-only view of the per-prompt size accounting computed
        at composer load time.

        Operators audit this pre-deployment to decide "can I add
        another rule without risk?". Telemetry consumers serialize
        via ``dataclasses.asdict`` on each value. Read-only copy
        prevents callers from mutating the snapshot in flight."""
        return dict(self._prompt_budgets)

    @staticmethod
    def _build_prompt_budgets(
        prompts: dict[str, str],
        *,
        soft_cap_kb: float,
        hard_cap_kb: float,
    ) -> dict[str, PromptBudget]:
        """Compute a ``PromptBudget`` for every loaded prompt.

        Sizes are UTF-8 byte counts (the wire size the LLM gateway
        sees), not character counts — multi-byte glyphs in a prompt
        would otherwise under-report against caps tuned for byte
        budgets."""
        soft_cap_bytes = int(soft_cap_kb * 1024)
        hard_cap_bytes = int(hard_cap_kb * 1024)
        return {
            name: PromptBudget(
                name=name,
                size_bytes=len(body.encode("utf-8")),
                soft_cap_bytes=soft_cap_bytes,
                hard_cap_bytes=hard_cap_bytes,
            )
            for name, body in prompts.items()
        }

    @staticmethod
    def _enforce_prompt_budgets(budgets: dict[str, PromptBudget]) -> None:
        """Apply soft/hard caps to ``system.md`` specifically.

        ``system.md`` is the only prompt the LLM sees verbatim at
        every compose; the other prompts (composition_bias, etc.)
        get concatenated AFTER and contribute additional budget
        pressure but are individually smaller. The hard-cap raise
        targets ``system.md`` because that's where the 12B-model
        instruction-drop-off problem manifests first.

        Other prompts get a soft-cap warning if they breach their
        own soft cap, but no hard-cap raise — they're rarely large
        enough to be at risk individually, and an operator may
        legitimately ship a long sub-prompt for a specialized
        deployment.
        """
        system = budgets.get("system")
        if system is None:
            # No system prompt loaded — the composer is unusable
            # for compose() calls, but the missing-file error is
            # already raised by ``_load_prompts``. Defensive no-op.
            return
        if not system.is_within_hard_cap:
            raise ComposerConfigurationError(
                f"system.md is {system.size_bytes} bytes "
                f"({system.size_bytes / 1024:.2f} KB), exceeding the "
                f"hard cap of {system.hard_cap_bytes} bytes "
                f"({system.hard_cap_bytes / 1024:.2f} KB). The 12B "
                f"local model (mistral-nemo) starts dropping later-"
                f"prompt instructions past this size — silent quality "
                f"drift, not a runtime crash. Trim the prompt or "
                f"raise ``prompt_hard_cap_kb`` in composer config "
                f"(only if your deployment uses a model that tolerates "
                f"larger prompts)."
            )
        if not system.is_within_soft_cap:
            log.warning(
                "system.md is %d bytes (%.2f KB), exceeding the soft "
                "cap of %d bytes (%.2f KB). The composer still starts, "
                "but a future rule addition risks crossing the hard "
                "cap. Plan a consolidation pass.",
                system.size_bytes,
                system.size_bytes / 1024,
                system.soft_cap_bytes,
                system.soft_cap_bytes / 1024,
            )
        for name, budget in budgets.items():
            if name == "system":
                continue
            if not budget.is_within_soft_cap:
                log.info(
                    "prompt %r is %d bytes (%.2f KB), above its soft "
                    "cap of %.2f KB. Not raising — only system.md "
                    "is hard-capped — but worth noting for future "
                    "consolidation.",
                    name,
                    budget.size_bytes,
                    budget.size_bytes / 1024,
                    budget.soft_cap_bytes / 1024,
                )


def _persisted_composition_summary(
    summary: CompositionSummary, novel_python: dict[str, str]
) -> dict:
    """Serialize a CompositionSummary to the dict persisted on the GeneratedArtifact.

    Extracted from the inline GenerationMetadata construction so the persisted shape —
    including the T6 ``env_manifest`` provenance stamp — is unit-testable without driving
    a full compose() (the store-backed compose tests are pre-existing-broken by spec-rot).
    """
    return {
        "steps_reused": summary.steps_reused,
        "steps_generated": summary.steps_generated,
        "steps_swapped": summary.steps_swapped,
        "summary_sentence": summary.summary_sentence,
        # T06: per-step categorization (for /workflows/diff) + the novel_python source
        # (for /workflows/novel_python).
        "step_categorizations": [
            {
                "step_id": s.step_id,
                "step_class": s.step_class,
                "category": s.category.value,
                "reason": s.reason,
                "retrieval_gap": s.retrieval_gap,
            }
            for s in summary.step_categorizations
        ],
        "review_notes": list(summary.review_notes),
        "novel_python_by_step": dict(novel_python),
        # C1: compose-validate-retry rounds (prompt-drift metric).
        "compose_retries": summary.compose_retries,
        # CPR: class-path auto-repairs (leaf-match repair-rate metric).
        "class_path_repairs": [
            {"step_id": s, "emitted": e, "resolved": r} for s, e, r in summary.class_path_repairs
        ],
        # T6: the env-manifest provenance STAMP — persisted so a stored artifact carries it
        # (Project B reads it), never silently dropped at the persistence boundary.
        "env_manifest": summary.env_manifest,
    }


__all__ = [
    "Composer",
    "ComposerConfigurationError",
    "ComposerResponseError",
    "REQUIRED_PROMPT_FILES",
]
