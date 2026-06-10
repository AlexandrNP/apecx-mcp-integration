"""TaxdumpFetchStep — nanobrain wrapper around ``fetch_taxdump``.

Wraps the existing procedural :func:`apecx_integration.synonym_dictionary.taxdump_fetcher.fetch_taxdump`
helper as a real nanobrain step so the dictionary-build pipeline can
run as a ``Workflow`` instead of as a console script.

Contract
--------
- Input: none (the fetch is a pure side-effect against NCBI's FTP).
  The step still declares an ``input_data_units`` entry called
  ``trigger`` so the framework's data-driven cascade has something
  to deposit a kick-off value into. The trigger value itself is
  ignored by ``process()``.
- Output: a single data unit ``taxdump_paths`` whose value is a dict
  ``{"nodes_path": <str>, "merged_path": <str>, "names_path": <str>,
  "delnodes_path": <str>}`` — the absolute, resolved string paths to
  the four extracted dump files. Strings rather than ``Path`` so the
  value serialises through any future remote data-unit transport.

  ``names_path`` + ``delnodes_path`` were added 2026-06-08 (SC-A2) when
  the dictionary build began ingesting all 7 NCBI name classes from
  ``names.dmp`` and surfacing deleted-taxon lookups via ``delnodes.dmp``.
  Downstream consumers that pre-date the change continue to work
  because ``DictionaryBuildStep._extract_taxdump_paths`` reads
  ``nodes_path`` + ``merged_path`` only and tolerates extra keys.

Idempotency
-----------
``fetch_taxdump`` is idempotent — when all four files already exist
under ``output_dir`` and ``force=False``, it skips the download. The
step inherits that behaviour without further work. A pre-SC-A2 cache
that contains only ``nodes.dmp`` + ``merged.dmp`` fails the
all-four-present check and is silently re-extracted from the cached
tarball (cheap — no re-download).

Framework compliance
--------------------
- Subclasses :class:`BaseStep`, implements ``process()`` only — never
  overrides ``execute``.
- The step owns its input/output data units and trigger; the workflow
  YAML owns the link to the next step.
- The step's :class:`TaxdumpFetchStepConfig` sets ``extra='forbid'``
  per the workspace rule so YAML typos in the step config raise
  rather than silently use defaults.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field

from apecx_integration.synonym_dictionary.taxdump_fetcher import fetch_taxdump

log = logging.getLogger(__name__)


def _default_taxdump_dir() -> str:
    """Resolve the default output directory at config-validation time.

    Order of precedence:

    1. ``APECX_TAXDUMP_DIR`` environment variable.
    2. ``~/.apecx/taxdump`` expanded against the caller's home dir.

    Returned as a string because the StepConfig field is typed ``str``
    so it round-trips cleanly through YAML.
    """
    env_value = os.environ.get("APECX_TAXDUMP_DIR")
    if env_value:
        return env_value
    return str(Path("~/.apecx/taxdump").expanduser())


class TaxdumpFetchStepConfig(StepConfig):
    """Config for :class:`TaxdumpFetchStep`.

    Adds three fields on top of :class:`StepConfig`:

    - ``output_dir`` — directory for ``nodes.dmp`` / ``merged.dmp``;
      defaults to ``${APECX_TAXDUMP_DIR:-~/.apecx/taxdump}`` resolved
      at validation time.
    - ``url`` — override the NCBI FTP URL (useful for tests or mirrors).
    - ``force`` — re-download even when both extracted files are present.

    ``extra='forbid'`` is set so YAML typos surface as validation errors
    instead of silently going to defaults — workspace policy.

    ``validate_assignment=False`` (vs. ConfigBase's ``True`` default) so
    the framework can attach its own post-instantiation attributes —
    notably ``source_path`` — via ``setattr``. Pydantic enforces
    ``extra='forbid'`` at *initial* validation regardless, so the
    workspace policy still catches YAML typos at load time.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
        use_enum_values=False,
        validate_assignment=False,
        str_strip_whitespace=False,
    )

    # Framework-set: ConfigBase.from_config() does
    # ``setattr(config_instance, 'source_path', str(config_path))`` after
    # validation. With ``extra='forbid'`` we must declare it explicitly
    # so the assignment doesn't raise. Optional + default=None so callers
    # constructing the model in-memory don't have to set it.
    source_path: str | None = Field(
        default=None,
        description="Framework-set path of the YAML the config was loaded from.",
    )

    output_dir: str = Field(
        default_factory=_default_taxdump_dir,
        description=(
            "Directory where nodes.dmp and merged.dmp will be written. "
            "Defaults to ${APECX_TAXDUMP_DIR:-~/.apecx/taxdump}."
        ),
    )
    url: str | None = Field(
        default=None,
        description=(
            "Optional override of the NCBI FTP URL; passed straight "
            "through to fetch_taxdump. None => use the module default."
        ),
    )
    force: bool = Field(
        default=False,
        description=(
            "Re-download and re-extract even if the four required dump "
            "files are already present under output_dir."
        ),
    )


class TaxdumpFetchStep(BaseStep):
    """Download NCBI taxdump and emit the extracted file paths.

    Expected ``process()`` input::

        {"trigger": <any>}      # value is ignored

    Return shape::

        {"taxdump_paths": {
            "nodes_path":    "/abs/path/to/nodes.dmp",
            "merged_path":   "/abs/path/to/merged.dmp",
            "names_path":    "/abs/path/to/names.dmp",
            "delnodes_path": "/abs/path/to/delnodes.dmp",
        }}
    """

    COMPONENT_TYPE: str = "taxdump_fetch_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return TaxdumpFetchStepConfig

    @classmethod
    def extract_component_config(cls, config: TaxdumpFetchStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "output_dir": config.output_dir,
            "url": config.url,
            "force": config.force,
        }

    def _init_from_config(
        self,
        config: TaxdumpFetchStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._output_dir: str = component_config["output_dir"]
        self._url: str | None = component_config.get("url")
        self._force: bool = bool(component_config.get("force", False))

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        # input_data is ignored — fetch_taxdump is parameterless from the
        # workflow's perspective; the trigger DU just unblocks the cascade.
        log.info(
            "TaxdumpFetchStep %s: fetching taxdump into %s (force=%s)",
            self.name,
            self._output_dir,
            self._force,
        )

        # fetch_taxdump is synchronous (httpx blocking + tarfile). Run it on
        # a thread so we don't freeze the asyncio loop while ~50 MB streams
        # in.
        import asyncio  # noqa: PLC0415 — local import keeps module-import side effects minimal

        kwargs_for_fetch: dict[str, Any] = {"force": self._force}
        if self._url is not None:
            kwargs_for_fetch["url"] = self._url

        nodes_path, merged_path, names_path, delnodes_path = await asyncio.to_thread(
            fetch_taxdump,
            self._output_dir,
            **kwargs_for_fetch,
        )

        result = {
            "nodes_path": str(nodes_path),
            "merged_path": str(merged_path),
            "names_path": str(names_path),
            "delnodes_path": str(delnodes_path),
        }
        log.info(
            "TaxdumpFetchStep %s: emitting %s",
            self.name,
            sorted(result.keys()),
        )
        return {"taxdump_paths": result}
