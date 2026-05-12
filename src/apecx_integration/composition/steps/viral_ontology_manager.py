"""Viral immunology ontology management utilities.

Provides configurable virus taxonomy, protein classification, and immunology
concept detection for the viral immunology query classifier.

Design principles:
- Configuration-driven (YAML-based) rather than hardcoded terms
- Extensible to new viruses and research types
- Supports synonym matching and fuzzy detection
- Framework-agnostic utilities (no nanobrain dependencies)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


@dataclass
class ViralFamily:
    """Represents a viral family with associated metadata."""

    name: str
    aliases: list[str] = field(default_factory=list)
    common_proteins: list[str] = field(default_factory=list)
    research_contexts: list[str] = field(default_factory=list)


@dataclass
class VirusEntry:
    """Represents a specific virus with detection patterns."""

    name: str
    family: str
    aliases: list[str] = field(default_factory=list)
    proteins: list[str] = field(default_factory=list)


@dataclass
class ImmunologyTerm:
    """Represents an immunology concept with context."""

    name: str
    category: str  # epitope, antibody, vaccine, etc.
    keywords: list[str] = field(default_factory=list)
    research_type: str = "general"


@dataclass
class QueryClassification:
    """Result of viral immunology query classification."""

    query: str
    virus_family: str | None = None
    virus_names: list[str] = field(default_factory=list)
    proteins_of_interest: list[str] = field(default_factory=list)
    research_type: str = "general"
    immunology_concepts: list[str] = field(default_factory=list)
    confidence: float = 0.0
    is_viral_immunology: bool = False


class ViralOntologyManager:
    """Manages viral immunology ontology and query classification.

    Loads virus taxonomy, protein classifications, and immunology concepts
    from YAML configuration. Provides methods for intelligent query analysis
    that can handle any virus family.
    """

    def __init__(self, ontology_config: dict[str, Any]):
        """Initialize from ontology configuration dict."""
        self.viral_families: dict[str, ViralFamily] = {}
        self.viruses: dict[str, VirusEntry] = {}
        self.immunology_terms: dict[str, ImmunologyTerm] = {}
        self.protein_terms: list[str] = []

        self._load_ontology(ontology_config)

    @classmethod
    def from_yaml(cls, config_path: Path) -> ViralOntologyManager:
        """Load ontology from YAML configuration file."""
        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        return cls(config)

    def _load_ontology(self, config: dict[str, Any]) -> None:
        """Load virus families, viruses, and immunology terms from config."""
        # Load viral families
        families_config = config.get("viral_families", {})
        for family_name, family_data in families_config.items():
            self.viral_families[family_name.lower()] = ViralFamily(
                name=family_name,
                aliases=[alias.lower() for alias in family_data.get("aliases", [])],
                common_proteins=[p.lower() for p in family_data.get("common_proteins", [])],
                research_contexts=family_data.get("research_contexts", []),
            )

        # Load specific viruses
        viruses_config = config.get("viruses", {})
        for virus_name, virus_data in viruses_config.items():
            self.viruses[virus_name.lower()] = VirusEntry(
                name=virus_name,
                family=virus_data.get("family", "").lower(),
                aliases=[alias.lower() for alias in virus_data.get("aliases", [])],
                proteins=[p.lower() for p in virus_data.get("proteins", [])],
            )

        # Load immunology terms
        immunology_config = config.get("immunology_terms", {})
        for term_name, term_data in immunology_config.items():
            self.immunology_terms[term_name.lower()] = ImmunologyTerm(
                name=term_name,
                category=term_data.get("category", "general"),
                keywords=[kw.lower() for kw in term_data.get("keywords", [])],
                research_type=term_data.get("research_type", "general"),
            )

        # Build comprehensive protein terms list
        self.protein_terms = list(config.get("protein_terms", []))
        for family in self.viral_families.values():
            self.protein_terms.extend(family.common_proteins)
        for virus in self.viruses.values():
            self.protein_terms.extend(virus.proteins)
        self.protein_terms = [p.lower() for p in set(self.protein_terms)]

        log.info(
            "Loaded viral ontology: %d families, %d viruses, %d immunology terms, %d protein terms",
            len(self.viral_families),
            len(self.viruses),
            len(self.immunology_terms),
            len(self.protein_terms),
        )

    def classify_query(self, query: str) -> QueryClassification:
        """Classify a query for viral immunology research intent.

        Returns structured classification with virus detection, protein
        identification, research type classification, and confidence scoring.
        """
        query_lower = query.lower()
        classification = QueryClassification(query=query)

        # Detect virus mentions
        detected_viruses = self._detect_viruses(query_lower)
        detected_families = self._detect_viral_families(query_lower)

        # Detect protein mentions
        detected_proteins = self._detect_proteins(query_lower)

        # Detect immunology concepts
        detected_immunology = self._detect_immunology_concepts(query_lower)

        # Determine research type from immunology concepts
        research_type = self._determine_research_type(detected_immunology)

        # Calculate confidence based on detection strength
        confidence = self._calculate_confidence(
            detected_viruses, detected_families, detected_proteins, detected_immunology
        )

        # Populate classification
        classification.virus_names = [v.name for v in detected_viruses]

        # Determine virus family - use direct detection first, then infer from viruses
        if detected_families:
            classification.virus_family = detected_families[0].name
        elif detected_viruses:
            # Infer family from detected viruses
            virus_families = {v.family for v in detected_viruses if v.family}
            if virus_families:
                classification.virus_family = next(iter(virus_families))  # Take first family
        else:
            classification.virus_family = None
        classification.proteins_of_interest = list(detected_proteins)
        classification.immunology_concepts = [term.name for term in detected_immunology]
        classification.research_type = research_type
        classification.confidence = confidence
        classification.is_viral_immunology = confidence >= 0.3  # Minimum confidence threshold

        return classification

    def _detect_viruses(self, query_lower: str) -> list[VirusEntry]:
        """Detect specific virus mentions in query."""
        detected = []
        for virus in self.viruses.values():
            # Check virus name and aliases
            if virus.name.lower() in query_lower or any(
                alias in query_lower for alias in virus.aliases
            ):
                detected.append(virus)
        return detected

    def _detect_viral_families(self, query_lower: str) -> list[ViralFamily]:
        """Detect viral family mentions in query."""
        detected = []
        for family in self.viral_families.values():
            if family.name.lower() in query_lower or any(
                alias in query_lower for alias in family.aliases
            ):
                detected.append(family)
        return detected

    def _detect_proteins(self, query_lower: str) -> list[str]:
        """Detect protein mentions in query."""
        detected = []
        for protein in self.protein_terms:
            if protein in query_lower:
                detected.append(protein)
        return list(set(detected))  # Remove duplicates

    def _detect_immunology_concepts(self, query_lower: str) -> list[ImmunologyTerm]:
        """Detect immunology concept mentions in query."""
        detected = []
        for term in self.immunology_terms.values():
            # Check main term name
            if term.name.lower() in query_lower or any(
                keyword in query_lower for keyword in term.keywords
            ):
                detected.append(term)
        return detected

    def _determine_research_type(self, immunology_terms: list[ImmunologyTerm]) -> str:
        """Determine research type based on detected immunology concepts."""
        if not immunology_terms:
            return "general"

        # Priority-based classification
        research_types = [term.research_type for term in immunology_terms]

        if "epitope_analysis" in research_types:
            return "epitope_analysis"
        elif "vaccine_development" in research_types:
            return "vaccine_development"
        elif "antibody_analysis" in research_types:
            return "antibody_analysis"
        elif "structural_analysis" in research_types:
            return "structural_analysis"
        else:
            return "general_immunology"

    def _calculate_confidence(
        self,
        viruses: list[VirusEntry],
        families: list[ViralFamily],
        proteins: list[str],
        immunology: list[ImmunologyTerm],
    ) -> float:
        """Calculate confidence score for viral immunology classification."""
        score = 0.0

        # Virus detection (40% of total confidence)
        if viruses:
            score += 0.4
        elif families:
            score += 0.25  # Family detection is less specific

        # Protein detection (30% of total confidence)
        if proteins:
            score += 0.3

        # Immunology concepts (30% of total confidence)
        if immunology:
            score += 0.3

        return min(score, 1.0)  # Cap at 1.0


def load_default_ontology() -> ViralOntologyManager:
    """Load the default viral immunology ontology configuration.

    Returns a configured ViralOntologyManager with comprehensive virus
    taxonomy and immunology concept definitions.
    """
    # Default configuration embedded for framework independence
    default_config = {
        "viral_families": {
            "Alphaviridae": {
                "aliases": ["alphavirus", "togaviridae"],
                "common_proteins": ["envelope", "glycoprotein", "e1", "e2", "capsid"],
                "research_contexts": ["vector_borne", "encephalitis", "arthritis"],
            },
            "Coronaviridae": {
                "aliases": ["coronavirus", "covid"],
                "common_proteins": ["spike", "s protein", "envelope", "nucleocapsid", "membrane"],
                "research_contexts": ["respiratory", "pandemic", "sars"],
            },
            "Orthomyxoviridae": {
                "aliases": ["influenza", "flu"],
                "common_proteins": ["hemagglutinin", "neuraminidase", "ha", "na", "nucleoprotein"],
                "research_contexts": ["seasonal", "pandemic", "respiratory"],
            },
            "Flaviviridae": {
                "aliases": ["flavivirus"],
                "common_proteins": ["envelope", "capsid", "membrane", "ns1", "ns3"],
                "research_contexts": ["vector_borne", "hemorrhagic_fever", "encephalitis"],
            },
        },
        "viruses": {
            "Eastern Equine Encephalitis Virus": {
                "family": "Alphaviridae",
                "aliases": [
                    "eeev",
                    "eastern equine encephalitis",
                    "eastern equine encephalitis virus",
                ],
                "proteins": ["e1 glycoprotein", "e2 glycoprotein", "envelope protein"],
            },
            "SARS-CoV-2": {
                "family": "Coronaviridae",
                "aliases": ["covid-19", "coronavirus", "sars-cov-2"],
                "proteins": ["spike protein", "receptor binding domain", "rbd"],
            },
            "Influenza A": {
                "family": "Orthomyxoviridae",
                "aliases": ["flu a", "influenza a virus", "h1n1", "h3n2"],
                "proteins": ["hemagglutinin", "neuraminidase"],
            },
            "Zika Virus": {
                "family": "Flaviviridae",
                "aliases": ["zikv", "zika", "zika virus"],
                "proteins": ["envelope protein", "membrane protein"],
            },
            "HIV": {
                "family": "Retroviridae",
                "aliases": ["human immunodeficiency virus", "hiv-1"],
                "proteins": ["envelope", "gp120", "gp41", "capsid"],
            },
        },
        "immunology_terms": {
            "epitope": {
                "category": "structural",
                "keywords": ["epitopes", "binding site", "antigenic site"],
                "research_type": "epitope_analysis",
            },
            "neutralizing": {
                "category": "functional",
                "keywords": ["neutralizing antibody", "neutralization", "virus neutralization"],
                "research_type": "antibody_analysis",
            },
            "vaccine": {
                "category": "therapeutic",
                "keywords": ["vaccination", "immunization", "vaccine target"],
                "research_type": "vaccine_development",
            },
            "antibody": {
                "category": "immune_response",
                "keywords": ["antibodies", "immunoglobulin", "binding"],
                "research_type": "antibody_analysis",
            },
            "conserved": {
                "category": "evolutionary",
                "keywords": ["conservation", "conserved region", "preserved"],
                "research_type": "structural_analysis",
            },
        },
        "protein_terms": [
            "envelope",
            "capsid",
            "membrane",
            "spike",
            "glycoprotein",
            "nucleocapsid",
            "polyprotein",
            "protease",
            "polymerase",
        ],
    }

    return ViralOntologyManager(default_config)
