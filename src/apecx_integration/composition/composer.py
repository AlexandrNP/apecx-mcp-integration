"""LLM composer — Phase 1 skeleton (T-COMP per docs/composer_task_spec.md).

**Phase 1 is intentionally non-functional for ``compose()``.**
``Composer.from_config(yaml_path)`` works: it loads a ``ComposerConfig``
and verifies the configured ``prompt_dir`` has the three required
prompt files. ``compose()`` raises ``NotImplementedError`` with a
message naming the spec phase that ships it.

Why ship a non-functional skeleton now (per spec §6 P1 exit criterion):

1. Validates that ``ComposerConfig`` is loadable from YAML end-to-end.
2. Reserves the class's public surface so Phase 2 can fill in
   ``compose()`` without also having to decide on the API.
3. Enforces AC6 (no inline prompt strings) from the first commit —
   the prompt dir is loaded but its contents are not embedded in
   Python source.

Anti-pattern alert: this is a SCAFFOLD, not a STUB. The difference
is that a stub pretends to succeed (returns an empty ComposedWorkflow,
silently does nothing); a scaffold fails loudly with a clear message
pointing at the phase that implements it. ``compose()`` raises with
a spec citation so anyone calling it in Phase-1 code gets a readable
failure.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from apecx_integration.composition.composer_schemas import (
    ComposedWorkflow,
    ComposerConfig,
)

log = logging.getLogger(__name__)


REQUIRED_PROMPT_FILES: tuple[str, ...] = (
    "system.md",
    "composition_bias.md",
    "novel_python_flagging.md",
)


class ComposerConfigurationError(ValueError):
    """Raised when a composer config is structurally wrong.

    Distinct from the generic ValueError pydantic raises so callers
    can catch composer-specific config problems without also catching
    every unrelated validation error.
    """


class Composer:
    """LLM-backed workflow composer (T-COMP).

    Phase 1: construction + config loading only. ``compose()`` is a
    documented NotImplementedError until Phase 2 lands.
    """

    def __init__(self, config: ComposerConfig) -> None:
        # Direct construction is fine (no nanobrain Step ceremony needed —
        # composer is a plain apecx_integration class). from_config is the
        # preferred entry point because it gives YAML-file loading.
        self._config = config
        self._prompts: dict[str, str] = self._load_prompts(config.prompt_dir)
        log.info(
            "Composer initialized (Phase 1 skeleton): library=%s llm=%s prompts=%d",
            config.library_version,
            config.llm_model,
            len(self._prompts),
        )

    @classmethod
    def from_config(cls, config_path: str | Path) -> Composer:
        """Load a ``ComposerConfig`` from YAML and build the composer.

        Phase-1 exit criterion (spec §6 P1): this call must succeed
        against the bundled sample config ``composer_config.yml``.
        """
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

        # Resolve prompt_dir relative to the config file if it's not absolute,
        # so an operator-supplied config with `prompt_dir: composer_prompts/`
        # works regardless of the CWD pytest / uvicorn was launched from.
        prompt_dir_raw = raw.get("prompt_dir")
        if prompt_dir_raw is None:
            raise ComposerConfigurationError(
                "composer config missing required 'prompt_dir'"
            )
        prompt_dir = Path(prompt_dir_raw)
        if not prompt_dir.is_absolute():
            prompt_dir = (path.parent / prompt_dir).resolve()
        raw["prompt_dir"] = prompt_dir

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
        """Loaded prompt texts, keyed by filename without extension."""
        return dict(self._prompts)

    async def compose(self, prompt: str, context: dict[str, Any] | None = None) -> ComposedWorkflow:
        """Compose a workflow YAML from a natural-language prompt.

        **Not implemented in T-COMP Phase 1.** The skeleton exists so
        ``from_config`` + config discovery can be exercised ahead of the
        first real LLM call. Phase 2 replaces this body with a
        retrieval + LLM-call + YAML-parse + T13-scan pipeline.

        See ``docs/composer_task_spec.md`` §6 P2 for the exit criterion.
        """
        raise NotImplementedError(
            "Composer.compose() is unimplemented in T-COMP Phase 1. "
            "The Phase-2 implementation (retrieval + LLM call + T13 scan) "
            "is scoped in docs/composer_task_spec.md §6 P2."
        )

    # ---- internals ---------------------------------------------------------

    @classmethod
    def _load_prompts(cls, prompt_dir: Path) -> dict[str, str]:
        """Read every required prompt file under ``prompt_dir``.

        Returns ``{"system": "...", "composition_bias": "...", ...}`` keyed
        by filename without the ``.md`` suffix. Raises if any required
        file is missing — AC6 can't hold if the prompt dir is incomplete.
        """
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
            key = p.stem
            prompts[key] = p.read_text(encoding="utf-8")
        if missing:
            raise ComposerConfigurationError(
                f"prompt_dir {prompt_dir} missing required prompt files: "
                f"{sorted(missing)}"
            )
        return prompts
