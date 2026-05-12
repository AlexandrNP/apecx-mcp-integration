"""CGU-P0-T2 — role resolver for benchmark codegens.

The benchmark codegens (``tests/benchmarks/codegen/*.py``) previously
read their model name from env vars only. That worked for a single-
model baseline but does not scale: the codegen-uplift effort runs
multi-stage scaffolds where each stage may want a different model
(drafter / planner / reviewer), and the operator may want to override
some-but-not-all roles per deployment.

This module is the single resolution helper. Every codegen calls
``resolve_role("drafter", ...)`` (or ``"planner"``, ``"reviewer"``)
to get back a ``(model, base_url)`` pair.

Resolution order, narrowest-wins:

1. Explicit Python kwarg (test override; ``kwarg_model``).
2. Role-specific env var (``APECX_LLM_MODEL_<ROLE>``).
3. ``ComposerConfig.model_roles[<role>]`` entry from
   ``composer_config.yml``.
4. Generic env var (``APECX_LLM_MODEL``) — backward compat with
   single-model deployments.
5. Hardcoded role default (this module).

The config is loaded lazily on first call and cached process-wide;
tests that need a fresh load call ``_clear_role_cache()`` in a
fixture.
"""

from __future__ import annotations

import os
from pathlib import Path

# Hardcoded defaults — last line of defense, used only when every
# other source above also fails. These match the codegen-uplift plan
# (``docs/composer_codegen_uplift_plan.md`` §3).
_HARDCODED_DEFAULTS: dict[str, str] = {
    "drafter": "mistral-nemo:latest",
    "planner": "nemotron-3-nano:4b",
    "reviewer": "nemotron-3-nano:4b",
}

_DEFAULT_BASE_URL: str = "http://localhost:11434/v1"

_DEFAULT_CONFIG_PATH: Path = (
    Path(__file__).parent.parent.parent
    / "src"
    / "apecx_integration"
    / "composition"
    / "composer_config.yml"
)


_CONFIG_CACHE: dict[str, tuple[str, str | None]] | None = None


def _load_config_roles(config_path: Path | None = None) -> dict[str, tuple[str, str | None]]:
    """Load ``model_roles`` from ``composer_config.yml`` once per process.

    Returns ``{role: (model, base_url_or_None)}``. On any load
    failure (missing file, malformed YAML, pydantic rejection), the
    function returns an empty dict — resolution then falls back to
    env vars / hardcoded defaults. This is deliberate: a broken
    composer_config.yml should not break the benchmark harness's
    ability to run a smoke sweep.
    """
    global _CONFIG_CACHE
    if config_path is None and _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    path = config_path or _DEFAULT_CONFIG_PATH
    out: dict[str, tuple[str, str | None]] = {}
    try:
        import yaml  # noqa: PLC0415

        from apecx_integration.composition.composer_schemas import (  # noqa: PLC0415
            ComposerConfig,
        )

        raw = yaml.safe_load(path.read_text())
        # ``ComposerConfig`` requires several fields beyond model_roles.
        # We do NOT instantiate it here — we pull model_roles directly
        # from the raw YAML so a malformed sibling field (e.g. a bad
        # library_version) does not block role resolution.
        roles = raw.get("model_roles") or {}
        for name, entry in roles.items():
            if isinstance(entry, dict) and "model" in entry:
                out[name] = (entry["model"], entry.get("base_url"))
        # Round-trip each entry through pydantic only when the YAML
        # actually populated something — gives us extra='forbid'
        # typo-catch without spuriously failing on missing siblings.
        if out:
            from apecx_integration.composition.composer_schemas import (  # noqa: PLC0415
                ModelRoleConfig,
            )

            for model, base in out.values():
                # Validate field shape per role. extra='forbid' on
                # ModelRoleConfig catches "mdl: foo" or "url: bar".
                ModelRoleConfig(model=model, base_url=base)
            _ = ComposerConfig  # ComposerConfig import kept for downstream tooling
    except Exception:
        out = {}
    if config_path is None:
        _CONFIG_CACHE = out
    return out


def _clear_role_cache() -> None:
    """Reset the process-wide cache. Use in test fixtures that mutate
    the config on disk or in env vars between cases."""
    global _CONFIG_CACHE
    _CONFIG_CACHE = None


def resolve_role(
    role: str,
    *,
    kwarg_model: str | None = None,
    kwarg_base_url: str | None = None,
    config_path: Path | None = None,
) -> tuple[str, str]:
    """Return the ``(model, base_url)`` pair to use for ``role``.

    See module docstring for the resolution order. The returned
    ``base_url`` is always a concrete string; we never return
    ``None`` for the base_url since downstream LangChain clients
    expect a non-empty endpoint.
    """
    if role not in _HARDCODED_DEFAULTS:
        # Unknown role -> we still resolve, but the hardcoded fallback
        # branch will raise KeyError. Surface a clearer error here.
        raise KeyError(f"Unknown codegen role {role!r}. Known roles: {sorted(_HARDCODED_DEFAULTS)}")

    # 1. Explicit Python kwarg.
    if kwarg_model is not None:
        model = kwarg_model
    else:
        # 2. Role-specific env var.
        env_role = os.environ.get(f"APECX_LLM_MODEL_{role.upper()}")
        if env_role:
            model = env_role
        else:
            # 3. composer_config.yml model_roles entry.
            config_roles = _load_config_roles(config_path)
            cfg_entry = config_roles.get(role)
            if cfg_entry is not None:
                model = cfg_entry[0]
                # The config entry may also bind a base_url; keep it
                # for step 5 of base_url resolution.
                if kwarg_base_url is None and cfg_entry[1]:
                    kwarg_base_url = cfg_entry[1]
            else:
                # 4. Generic env var.
                env_generic = os.environ.get("APECX_LLM_MODEL")
                # 5. Hardcoded default.
                model = env_generic or _HARDCODED_DEFAULTS[role]

    # base_url: kwarg > generic env > config role-specific (handled in
    # the model branch above) > hardcoded default.
    if kwarg_base_url is not None:
        base_url = kwarg_base_url
    else:
        env_base = os.environ.get("APECX_LLM_BASE_URL")
        base_url = env_base or _DEFAULT_BASE_URL

    return model, base_url


__all__ = ["resolve_role", "_clear_role_cache"]
