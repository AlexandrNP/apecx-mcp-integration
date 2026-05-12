"""Nanobrain BaseStep for intelligent viral immunology query classification.

Replaces hardcoded virus detection with configurable ontology-driven
classification that can handle any virus family and immunology research type.

Input format:
    {"query": "What are the conserved epitopes on EEEV envelope glycoprotein?"}

Output format:
    {
        "query": str,
        "classification": {
            "virus_family": str | None,
            "virus_names": list[str],
            "proteins_of_interest": list[str],
            "research_type": str,
            "immunology_concepts": list[str],
            "confidence": float
        },
        "is_viral_immunology": bool
    }

Framework compliance:
- Proper BaseStep with from_config pattern
- Uses self.nb_logger for logging
- Implements async def process(), not execute()
- Follows nanobrain configuration patterns
"""

from __future__ import annotations

import logging
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

from apecx_integration.composition.steps.viral_ontology_manager import (
    ViralOntologyManager,
    load_default_ontology,
)

log = logging.getLogger(__name__)


class ViralImmunologyQueryClassifierStepConfig(StepConfig):
    """Configuration for ViralImmunologyQueryClassifierStep.

    Follows workspace rule: extra='forbid' to catch YAML typos at load time.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    # Framework tracking attribute
    source_path: str | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        """Strip framework keys before extra='forbid' validation."""
        if isinstance(data, dict):
            data.pop("class", None)
        return data

    # Ontology configuration
    ontology_config_path: str | None = Field(
        default=None,
        description="Path to viral ontology YAML. When None, uses embedded default ontology.",
    )

    minimum_confidence: float = Field(
        default=0.3, description="Minimum confidence threshold for viral immunology classification."
    )

    enable_fuzzy_matching: bool = Field(
        default=True, description="Enable fuzzy matching for virus and protein names."
    )


class ViralImmunologyQueryClassifierStep(BaseStep):
    """Intelligent viral immunology query classifier step.

    Replaces hardcoded virus detection with configurable ontology-driven
    classification. Can detect any virus family, protein targets, and
    immunology research types based on YAML configuration.

    Example usage:
        input: {"query": "COVID-19 spike protein epitopes for vaccine design"}
        output: {
            "query": "COVID-19 spike protein epitopes for vaccine design",
            "classification": {
                "virus_family": "Coronaviridae",
                "virus_names": ["SARS-CoV-2"],
                "proteins_of_interest": ["spike protein"],
                "research_type": "vaccine_development",
                "immunology_concepts": ["epitope", "vaccine"],
                "confidence": 0.85
            },
            "is_viral_immunology": True
        }
    """

    COMPONENT_TYPE: str = "viral_immunology_query_classifier_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return ViralImmunologyQueryClassifierStepConfig

    @classmethod
    def extract_component_config(
        cls, config: ViralImmunologyQueryClassifierStepConfig
    ) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "ontology_config_path": getattr(config, "ontology_config_path", None),
            "minimum_confidence": getattr(config, "minimum_confidence", 0.3),
            "enable_fuzzy_matching": getattr(config, "enable_fuzzy_matching", True),
        }

    def _init_from_config(
        self,
        config: ViralImmunologyQueryClassifierStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)

        # Load viral ontology
        ontology_path = component_config.get("ontology_config_path")
        if ontology_path:
            from pathlib import Path

            self._ontology_manager = ViralOntologyManager.from_yaml(Path(ontology_path))
            self.nb_logger.info(f"Loaded custom viral ontology from {ontology_path}")
        else:
            self._ontology_manager = load_default_ontology()
            self.nb_logger.info("Using default embedded viral ontology")

        self._min_confidence: float = float(component_config.get("minimum_confidence", 0.3))
        self._enable_fuzzy: bool = bool(component_config.get("enable_fuzzy_matching", True))

        self.nb_logger.info(
            f"{self.name}: initialized with min_confidence={self._min_confidence}, "
            f"fuzzy_matching={self._enable_fuzzy}"
        )

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        """Classify query for viral immunology research intent.

        Args:
            input_data: Must contain {"query": str} with the research query

        Returns:
            Classification result with virus detection, protein identification,
            research type, and confidence scoring.

        Raises:
            ValueError: If input_data is malformed or missing required fields
        """
        if not isinstance(input_data, dict):
            raise ValueError(
                f"ViralImmunologyQueryClassifierStep '{self.name}': "
                f"input_data must be a dict, got {type(input_data).__name__}"
            )

        # Handle framework wrapping (when called from trigger cascade)
        if "classifier_input" in input_data and "query" not in input_data:
            input_data = input_data["classifier_input"]

        query = input_data.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ValueError(
                f"ViralImmunologyQueryClassifierStep '{self.name}': "
                f"input_data must have non-empty 'query' string; got "
                f"{type(query).__name__}={query!r}"
            )

        query = query.strip()

        self.nb_logger.info(f"{self.name}: classifying query: {query[:100]}...")

        # Perform classification
        classification = self._ontology_manager.classify_query(query)

        # Apply minimum confidence threshold
        is_viral_immunology = classification.confidence >= self._min_confidence

        self.nb_logger.info(
            f"{self.name}: classification complete - "
            f"virus_family={classification.virus_family}, "
            f"research_type={classification.research_type}, "
            f"confidence={classification.confidence:.3f}, "
            f"is_viral_immunology={is_viral_immunology}"
        )

        # Convert to output format
        result = {
            "query": query,
            "classification": {
                "virus_family": classification.virus_family,
                "virus_names": classification.virus_names,
                "proteins_of_interest": classification.proteins_of_interest,
                "research_type": classification.research_type,
                "immunology_concepts": classification.immunology_concepts,
                "confidence": classification.confidence,
            },
            "is_viral_immunology": is_viral_immunology,
        }

        if getattr(self, "debug_mode", False):
            self.nb_logger.debug(f"{self.name}: full classification result: {result}")

        return result
