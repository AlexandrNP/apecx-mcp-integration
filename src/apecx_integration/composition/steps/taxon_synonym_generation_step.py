"""TaxonSynonymGenerationStep — LLM-driven candidate virus-name generation (taxon-resolution
fallback for viral_epitope_analysis).

This is the FIRST step of an OPTIONAL LLM-driven taxon-resolution fallback. It runs only when
the deterministic dict resolver did NOT already resolve a taxon (``canonical_iri`` carrying an
``NCBITaxon`` IRI). When it must run, it widens the set of names a downstream BV-BRC taxonomy
search will try: it seeds from the deterministic name extractor and asks the LLM for additional
candidate spellings (current scientific name, common names, acronyms, legacy synonyms).

RELIABILITY (the fallback must NEVER force the workflow to require an LLM, and must never break
a run on a data/network/LLM issue):
  - It does NOT set ``LLM_ROLE`` — the run-time requires_llm gate must not refuse the workflow
    because of this OPTIONAL step.
  - FAIL-LOUD only on non-dict input. Any other problem (no LLM reachable, an LLM error)
    DEGRADES LOUD to the deterministically-extracted seed names plus a NAMED note.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

from apecx_integration.agents._llm_config import preflight_llm_model
from apecx_integration.agents._llm_factory import build_chat_llm
from apecx_integration.agents.globus_search.taxonomy_resolver import extract_virus_names

log = logging.getLogger(__name__)

_INPUT_KEY = "synonym_gen_input"
_DEFAULT_PROMPT_FILENAME = "taxon_synonym_generation_prompt.yml"
# Cap on the synonym list handed downstream (bounds the BV-BRC taxonomy searches in step 2).
_MAX_SYNONYMS = 8


def _already_resolved(bundle: dict[str, Any]) -> bool:
    """True when the deterministic dict resolver already won (the fallback is then skipped)."""
    iri = bundle.get("canonical_iri")
    return isinstance(iri, str) and "NCBITaxon" in iri


def _load_system_prompt(filename: str) -> str:
    """Load the ``system_prompt`` block from a co-located YAML (no hardcoded prompts in code)."""
    import yaml

    pp = Path(__file__).parent / filename
    doc = yaml.safe_load(pp.read_text(encoding="utf-8")) or {}
    prompt = doc.get("system_prompt") if isinstance(doc, dict) else None
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError(f"prompt file {pp} must carry a non-empty 'system_prompt' string.")
    return prompt


def _dedup_ci(names: list[str]) -> list[str]:
    """De-duplicate names case-insensitively, dropping blanks, preserving first-seen order."""
    out: list[str] = []
    seen: set[str] = set()
    for n in names:
        if not isinstance(n, str):
            continue
        s = n.strip()
        key = s.lower()
        if s and key not in seen:
            seen.add(key)
            out.append(s)
    return out


def _parse_name_lines(text: str) -> list[str]:
    """Parse the LLM's one-name-per-line output: strip bullets/numbering + blanks."""
    out: list[str] = []
    for raw in (text or "").splitlines():
        line = re.sub(r"^\s*(?:[-*•]+|\d+[.)])\s*", "", raw.strip()).strip()
        if line:
            out.append(line)
    return out


class TaxonSynonymGenerationStepConfig(StepConfig):
    """Config — ``extra='forbid'`` (workspace rule): YAML typos raise at config-load time."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data


class TaxonSynonymGenerationStep(BaseStep):
    COMPONENT_TYPE: str = "taxon_synonym_generation_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]
    # An OPTIONAL LLM fallback (runs only on a dict-resolver miss; degrade-loud on LLM failure),
    # so it does NOT REQUIRE a server LLM. Declared 'none' explicitly because the source heuristic
    # (workflow_requires_llm) otherwise mis-flags it as an in-DAG LLM step and wrongly forces a
    # server-LLM requirement in desktop locus. See the module docstring + test_llm_policy.
    LLM_ROLE: str = "none"

    @classmethod
    def _get_config_class(cls):
        return TaxonSynonymGenerationStepConfig

    def _init_from_config(self, config, component_config, dependencies) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._system_prompt: str = _load_system_prompt(_DEFAULT_PROMPT_FILENAME)

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        bundle = dict(self._unwrap(input_data))
        if _already_resolved(bundle):
            return bundle

        query = bundle.get("query") or ""
        seeds = _dedup_ci([query, *extract_virus_names(query)])

        # No reachable/pulled LLM → degrade LOUD to the extracted seed names.
        try:
            preflight_llm_model()
        except Exception as exc:  # noqa: BLE001 - optional LLM; degrade-loud, never raise
            log.warning(
                "TaxonSynonymGenerationStep %s: LLM preflight failed (%s); using extracted "
                "names only.",
                self.name,
                exc,
            )
            bundle["taxon_synonyms"] = seeds
            bundle["taxon_synonym_note"] = "LLM unavailable; using extracted names only"
            return bundle

        try:
            llm = build_chat_llm(temperature=0.0, max_tokens=256)
            user = (
                f"Query: {query}\n\n"
                f"Candidate names already extracted: {', '.join(seeds) or '(none)'}\n\n"
                f"List up to 6 candidate virus names for the SAME virus."
            )
            resp = await asyncio.to_thread(
                llm.invoke, [SystemMessage(content=self._system_prompt), HumanMessage(content=user)]
            )
            candidates = _parse_name_lines(getattr(resp, "content", "") or "")
            bundle["taxon_synonyms"] = _dedup_ci([*seeds, *candidates])[:_MAX_SYNONYMS]
        except Exception as exc:  # noqa: BLE001 - optional LLM; degrade-loud, never raise
            log.warning(
                "TaxonSynonymGenerationStep %s: synonym generation failed (%s); using extracted "
                "names only.",
                self.name,
                exc,
            )
            bundle["taxon_synonyms"] = seeds
            bundle["taxon_synonym_note"] = (
                f"LLM synonym generation failed ({type(exc).__name__}); using extracted names only"
            )
        return bundle

    def _unwrap(self, input_data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(input_data, dict):
            raise ValueError(
                f"TaxonSynonymGenerationStep '{self.name}': input_data must be a dict, got "
                f"{type(input_data).__name__}"
            )
        # Single-key trigger-envelope unwrap (the framework delivers {synonym_gen_input: payload}).
        if "query" not in input_data and len(input_data) == 1:
            only = next(iter(input_data.values()))
            if isinstance(only, dict):
                return only
        return input_data


__all__ = ["TaxonSynonymGenerationStep", "TaxonSynonymGenerationStepConfig"]
