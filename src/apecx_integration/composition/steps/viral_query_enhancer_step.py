"""Query enhancement step for viral immunology analysis.

Bridges the viral classifier and unlimited assembly steps by taking
classification results and structuring them into the format expected
by the assembly step for optimal multi-source retrieval.

Input format (from viral classifier):
    {
        "query": str,
        "classification": {
            "virus_family": str,
            "virus_names": list[str],
            "proteins_of_interest": list[str],
            "research_type": str,
            "immunology_concepts": list[str],
            "confidence": float
        },
        "is_viral_immunology": bool
    }

Output format (for unlimited assembly):
    {
        "query": str,
        "entities": [{"name": str, "type": str}, ...],
        "query_terms": list[str]
    }

Framework compliance:
- Proper BaseStep with from_config pattern
- Uses self.nb_logger for logging
- Implements async def process()
"""

from __future__ import annotations

import logging
from typing import Any

from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field, model_validator

log = logging.getLogger(__name__)


class ViralQueryEnhancerStepConfig(StepConfig):
    """Configuration for ViralQueryEnhancerStep."""

    model_config = ConfigDict(extra="forbid", validate_assignment=False)

    source_path: str | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def _strip_framework_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data.pop("class", None)
        return data

    # Enhancement configuration
    include_family_terms: bool = Field(
        default=True, description="Include viral family names in query terms for broader search."
    )

    enhance_protein_terms: bool = Field(
        default=True, description="Expand protein terms with common synonyms and variants."
    )

    boost_immunology_concepts: bool = Field(
        default=True, description="Add related immunology terms to improve retrieval."
    )


class ViralQueryEnhancerStep(BaseStep):
    """Query enhancement step for viral immunology analysis.

    Takes viral classification results and enhances the query with structured
    entities and terms for optimal multi-source data retrieval.

    Bridges between the viral classifier and unlimited assembly steps.
    """

    COMPONENT_TYPE: str = "viral_query_enhancer_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return ViralQueryEnhancerStepConfig

    @classmethod
    def extract_component_config(cls, config: ViralQueryEnhancerStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "include_family_terms": getattr(config, "include_family_terms", True),
            "enhance_protein_terms": getattr(config, "enhance_protein_terms", True),
            "boost_immunology_concepts": getattr(config, "boost_immunology_concepts", True),
        }

    def _init_from_config(
        self,
        config: ViralQueryEnhancerStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)

        self._include_family: bool = bool(component_config.get("include_family_terms", True))
        self._enhance_proteins: bool = bool(component_config.get("enhance_protein_terms", True))
        self._boost_immunology: bool = bool(component_config.get("boost_immunology_concepts", True))

        self.nb_logger.info(
            f"{self.name}: initialized query enhancer - "
            f"family_terms={self._include_family}, "
            f"protein_enhancement={self._enhance_proteins}, "
            f"immunology_boost={self._boost_immunology}"
        )

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        """Enhance query with classification results for optimal retrieval.

        Args:
            input_data: Classification results from viral classifier

        Returns:
            Enhanced query structure for unlimited assembly step

        Raises:
            ValueError: If input data is malformed or classification failed
        """
        if not isinstance(input_data, dict):
            raise ValueError(
                f"ViralQueryEnhancerStep '{self.name}': "
                f"input_data must be a dict, got {type(input_data).__name__}"
            )

        # Handle framework wrapping
        if "enhancer_input" in input_data and "query" not in input_data:
            input_data = input_data["enhancer_input"]

        # Validate required fields
        query = input_data.get("query")
        classification = input_data.get("classification")
        is_viral_immunology = input_data.get("is_viral_immunology", False)

        if not isinstance(query, str) or not query.strip():
            raise ValueError(
                f"ViralQueryEnhancerStep '{self.name}': input must have non-empty 'query' string"
            )

        if not is_viral_immunology:
            raise ValueError(
                f"ViralQueryEnhancerStep '{self.name}': "
                f"query was not classified as viral immunology research"
            )

        if not isinstance(classification, dict):
            raise ValueError(
                f"ViralQueryEnhancerStep '{self.name}': 'classification' must be a dict"
            )

        query = query.strip()
        self.nb_logger.info(f"{self.name}: enhancing query: {query[:80]}...")

        # Extract classification components
        virus_family = classification.get("virus_family")
        virus_names = classification.get("virus_names", [])
        proteins = classification.get("proteins_of_interest", [])
        research_type = classification.get("research_type", "general")
        immunology_concepts = classification.get("immunology_concepts", [])
        confidence = classification.get("confidence", 0.0)

        # Build enhanced entities list
        entities = []

        # Add virus entities
        for virus_name in virus_names:
            entities.append({"name": virus_name, "type": "virus"})

        # Add viral family if configured and available
        if self._include_family and virus_family and virus_family not in virus_names:
            entities.append({"name": virus_family, "type": "viral_family"})

        # Add protein entities
        enhanced_proteins = (
            self._enhance_protein_terms(proteins) if self._enhance_proteins else proteins
        )
        for protein in enhanced_proteins:
            entities.append({"name": protein, "type": "protein"})

        # Add immunology concept entities
        enhanced_immunology = (
            self._boost_immunology_terms(immunology_concepts, research_type)
            if self._boost_immunology
            else immunology_concepts
        )
        for concept in enhanced_immunology:
            entities.append({"name": concept, "type": "immunology_concept"})

        # Build comprehensive query terms list
        query_terms = []
        query_terms.extend(virus_names)
        query_terms.extend(enhanced_proteins)
        query_terms.extend(enhanced_immunology)

        if self._include_family and virus_family:
            query_terms.append(virus_family)

        # Remove duplicates while preserving order
        query_terms = list(dict.fromkeys(query_terms))

        # Remove empty terms
        query_terms = [term for term in query_terms if term and term.strip()]

        self.nb_logger.info(
            f"{self.name}: enhanced query - "
            f"entities={len(entities)}, query_terms={len(query_terms)}, "
            f"research_type={research_type}, confidence={confidence:.3f}"
        )

        if self.debug_mode:
            self.nb_logger.debug(f"{self.name}: entities={entities}, query_terms={query_terms}")

        return {
            "query": query,
            "entities": entities,
            "query_terms": query_terms,
            # Include original classification for downstream use
            "original_classification": classification,
        }

    def _enhance_protein_terms(self, proteins: list[str]) -> list[str]:
        """Enhance protein terms with common synonyms and variants.

        Adds common protein synonyms to improve retrieval coverage.
        """
        enhanced = list(proteins)  # Start with original terms

        # Common protein synonym mappings
        synonyms = {
            "spike": ["spike protein", "s protein", "surface glycoprotein"],
            "envelope": ["envelope protein", "env protein", "e protein"],
            "capsid": ["capsid protein", "nucleocapsid", "core protein"],
            "membrane": ["membrane protein", "m protein"],
            "glycoprotein": ["gp", "surface protein"],
            "hemagglutinin": ["ha", "h protein"],
            "neuraminidase": ["na", "n protein"],
            "nucleoprotein": ["np", "nucleocapsid protein"],
        }

        for protein in proteins:
            protein_lower = protein.lower()
            for base_term, variants in synonyms.items():
                if base_term in protein_lower:
                    enhanced.extend(variants)

        return list(dict.fromkeys(enhanced))  # Remove duplicates

    def _boost_immunology_terms(self, concepts: list[str], research_type: str) -> list[str]:
        """Boost immunology terms with related concepts.

        Adds related immunology terms based on research type to improve
        retrieval of relevant scientific literature and data.
        """
        enhanced = list(concepts)  # Start with original terms

        # Research type-specific term boosting
        type_boosts = {
            "epitope_analysis": [
                "epitope mapping",
                "antigenic site",
                "binding domain",
                "linear epitope",
                "conformational epitope",
            ],
            "vaccine_development": [
                "vaccine target",
                "immunogen",
                "vaccine design",
                "protective immunity",
                "immune response",
            ],
            "antibody_analysis": [
                "monoclonal antibody",
                "neutralizing antibody",
                "antibody binding",
                "immunoglobulin",
                "antigen recognition",
            ],
            "structural_analysis": [
                "protein structure",
                "crystal structure",
                "3d structure",
                "molecular modeling",
                "structural biology",
            ],
        }

        if research_type in type_boosts:
            enhanced.extend(type_boosts[research_type])

        # General immunology term boosting
        concept_boosts = {
            "epitope": ["antigenic determinant", "immunogenic region"],
            "antibody": ["immunoglobulin", "monoclonal"],
            "vaccine": ["immunization", "vaccination"],
            "neutralizing": ["neutralization", "virus neutralization"],
            "conserved": ["conservation", "preserved region"],
        }

        for concept in concepts:
            concept_lower = concept.lower()
            for base_term, boosts in concept_boosts.items():
                if base_term in concept_lower:
                    enhanced.extend(boosts)

        return list(dict.fromkeys(enhanced))  # Remove duplicates
