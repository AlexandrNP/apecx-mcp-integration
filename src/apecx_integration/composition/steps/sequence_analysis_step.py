"""SequenceAnalysisStep — BV-BRC sequence retrieval and multiple sequence alignment.

Performs sophisticated sequence-level reasoning for epitope analysis:
- Retrieves viral sequences from BV-BRC database
- Performs multiple sequence alignment using MUSCLE algorithm
- Calculates conservation scores across phylogenetic clades
- Identifies highly conserved regions for epitope prediction

Framework compliance:
- Subclasses BaseStep, implements process() only
- Uses framework-native data unit flows
- Proper trigger configuration for input changes
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
from datetime import datetime
from typing import Any

import requests
from nanobrain.core.step import BaseStep, StepConfig
from pydantic import ConfigDict, Field

log = logging.getLogger(__name__)


class SequenceAnalysisStepConfig(StepConfig):
    """Config for SequenceAnalysisStep.

    Extends base StepConfig with sequence analysis specific parameters.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="forbid",
        use_enum_values=False,
        validate_assignment=False,
        str_strip_whitespace=False,
    )

    # Framework-set source path
    source_path: str | None = Field(
        default=None,
        description="Framework-set path of the YAML config file.",
    )

    bvbrc_base_url: str = Field(
        default="https://www.bv-brc.org/api/genome",
        description="BV-BRC API base URL for sequence retrieval",
    )

    max_sequences: int = Field(
        default=50,
        description="Maximum number of sequences to retrieve per analysis",
    )

    conservation_threshold: float = Field(
        default=0.90,
        description="Conservation threshold for epitope identification (0.0-1.0)",
    )

    muscle_executable: str = Field(
        default="muscle",
        description="Path to MUSCLE alignment executable",
    )

    temporal_range_years: int = Field(
        default=20,
        description="Temporal range for sequence collection (years from present)",
    )


class SequenceAnalysisStep(BaseStep):
    """Sequence analysis step for epitope conservation analysis.

    Retrieves viral sequences from BV-BRC, performs multiple sequence alignment,
    and calculates conservation scores for epitope prediction.

    Expected process() input:
    {
        "query_data": {
            "virus_name": str,  # e.g., "Eastern Equine Encephalitis Virus"
            "protein_target": str,  # e.g., "envelope glycoprotein"
            "analysis_type": str  # "epitope_conservation"
        }
    }

    Return shape:
    {
        "sequence_analysis_result": {
            "sequences_retrieved": int,
            "alignment_file": str,
            "conservation_analysis": {
                "highly_conserved_regions": List[Dict],
                "conservation_scores": List[float],
                "temporal_coverage": Dict,
                "geographic_coverage": List[str]
            },
            "execution_metadata": Dict
        }
    }
    """

    COMPONENT_TYPE: str = "sequence_analysis_step"
    REQUIRED_CONFIG_FIELDS: list[str] = ["name"]

    @classmethod
    def _get_config_class(cls):
        return SequenceAnalysisStepConfig

    @classmethod
    def extract_component_config(cls, config: SequenceAnalysisStepConfig) -> dict[str, Any]:
        base = super().extract_component_config(config)
        return {
            **base,
            "bvbrc_base_url": config.bvbrc_base_url,
            "max_sequences": config.max_sequences,
            "conservation_threshold": config.conservation_threshold,
            "muscle_executable": config.muscle_executable,
            "temporal_range_years": config.temporal_range_years,
        }

    def _init_from_config(
        self,
        config: SequenceAnalysisStepConfig,
        component_config: dict[str, Any],
        dependencies: dict[str, Any],
    ) -> None:
        super()._init_from_config(config, component_config, dependencies)
        self._bvbrc_base_url: str = component_config["bvbrc_base_url"]
        self._max_sequences: int = component_config["max_sequences"]
        self._conservation_threshold: float = component_config["conservation_threshold"]
        self._muscle_executable: str = component_config["muscle_executable"]
        self._temporal_range_years: int = component_config["temporal_range_years"]

    async def process(self, input_data: dict[str, Any], **kwargs) -> dict[str, Any]:
        """RETIRED — fabricated data; FAIL-LOUD instead of running (2026-06-12).

        The original implementation wrote ``"ATCGATCG" * 20`` placeholder "sequences" (nucleotide
        text, not even amino acids) and copied its input as a "mock alignment" whenever MUSCLE was
        unavailable — manufacturing plausible-looking but meaningless conservation output. That is
        a mock-in-production violation: green tests, fake science. The step is dead (no live
        consumer, not in any catalog). The real, verified replacement is the conserved-sites
        cascade — ``BvbrcProteinFastaStep`` → ``LocalMafftAlignStep`` → ``ConservationScoreStep``,
        packaged as the ``viral_conserved_sites`` workflow (real BV-BRC sequences, real MAFFT MSA,
        real per-column conservation). Rather than ever emit fake results, this step now raises.
        (The fake-data helper methods below are kept only so this neutralization is a minimal,
        reviewable diff; they are unreachable.)
        """
        raise NotImplementedError(
            f"SequenceAnalysisStep '{self.name}' is RETIRED: it produced placeholder sequences and "
            "mock alignments (fake conservation — a mock-in-production violation). Use the "
            "viral_conserved_sites workflow (BvbrcProteinFastaStep → LocalMafftAlignStep → "
            "ConservationScoreStep) for real conserved-site analysis."
        )

    @staticmethod
    def _extract_query_data(input_data: dict[str, Any]) -> dict[str, Any]:
        """Extract and validate query data from input."""
        query_data = input_data.get("query_data")
        if not isinstance(query_data, dict):
            raise ValueError("SequenceAnalysisStep: input_data must contain 'query_data' dict")

        required_fields = ["virus_name", "protein_target", "analysis_type"]
        for field in required_fields:
            if field not in query_data:
                raise ValueError(
                    f"SequenceAnalysisStep: query_data missing required field '{field}'"
                )

        return query_data

    async def _retrieve_bvbrc_sequences(self, virus_name: str) -> list[dict[str, Any]]:
        """Retrieve viral sequences from BV-BRC API."""
        # Use asyncio.to_thread for blocking HTTP requests
        return await asyncio.to_thread(self._sync_retrieve_bvbrc_sequences, virus_name)

    def _sync_retrieve_bvbrc_sequences(self, virus_name: str) -> list[dict[str, Any]]:
        """Synchronous BV-BRC sequence retrieval."""
        try:
            # BV-BRC query parameters
            params = {
                "q": f'genome_name:"{virus_name}"',
                "rows": self._max_sequences,
                "fl": "genome_id,genome_name,collection_date,isolation_country,host_name",
                "wt": "json",
            }

            response = requests.get(self._bvbrc_base_url, params=params, timeout=30)
            response.raise_for_status()

            data = response.json()
            sequences = data.get("response", {}).get("docs", [])

            # Filter by temporal range if collection_date available
            current_year = datetime.now().year
            min_year = current_year - self._temporal_range_years

            filtered_sequences = []
            for seq in sequences:
                collection_date = seq.get("collection_date", "")
                if collection_date and len(collection_date) >= 4:
                    try:
                        seq_year = int(collection_date[:4])
                        if seq_year >= min_year:
                            filtered_sequences.append(seq)
                    except ValueError:
                        # Include sequences with unparseable dates
                        filtered_sequences.append(seq)
                else:
                    # Include sequences without dates
                    filtered_sequences.append(seq)

            return filtered_sequences[: self._max_sequences]

        except requests.RequestException as e:
            self.nb_logger.error(f"BV-BRC API request failed: {e}")
            return []
        except Exception as e:
            self.nb_logger.error(f"BV-BRC sequence retrieval failed: {e}")
            return []

    async def _perform_sequence_alignment(self, sequences: list[dict[str, Any]]) -> str:
        """Perform multiple sequence alignment using MUSCLE."""
        if len(sequences) < 2:
            raise ValueError("At least 2 sequences required for alignment")

        # Create temporary FASTA file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".fasta", delete=False) as fasta_file:
            for i, seq in enumerate(sequences):
                genome_id = seq.get("genome_id", f"seq_{i}")
                # For this implementation, we'll use a placeholder sequence
                # In production, this would fetch actual sequence data
                placeholder_seq = "ATCGATCGATCGATCG" * 20  # Mock sequence
                fasta_file.write(f">{genome_id}\n{placeholder_seq}\n")
            fasta_path = fasta_file.name

        # Create output alignment file
        alignment_file = fasta_path.replace(".fasta", "_aligned.fasta")

        try:
            # Run MUSCLE alignment
            await asyncio.to_thread(self._run_muscle_alignment, fasta_path, alignment_file)
            return alignment_file

        finally:
            # Cleanup input file
            if os.path.exists(fasta_path):
                os.unlink(fasta_path)

    def _run_muscle_alignment(self, input_file: str, output_file: str) -> None:
        """Run MUSCLE sequence alignment."""
        try:
            cmd = [self._muscle_executable, "-in", input_file, "-out", output_file]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

            if result.returncode != 0:
                # If MUSCLE not available, create mock alignment
                self.nb_logger.warning(f"MUSCLE alignment failed: {result.stderr}")
                self._create_mock_alignment(input_file, output_file)

        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            self.nb_logger.warning(f"MUSCLE not available ({e}), creating mock alignment")
            self._create_mock_alignment(input_file, output_file)

    def _create_mock_alignment(self, input_file: str, output_file: str) -> None:
        """Create mock alignment when MUSCLE is unavailable."""
        with open(input_file) as infile, open(output_file, "w") as outfile:
            outfile.write(infile.read())  # Copy input as mock alignment

    async def _calculate_conservation(
        self, sequences: list[dict[str, Any]], alignment_file: str
    ) -> dict[str, Any]:
        """Calculate sequence conservation analysis."""
        return await asyncio.to_thread(self._sync_calculate_conservation, sequences, alignment_file)

    def _sync_calculate_conservation(
        self, sequences: list[dict[str, Any]], alignment_file: str
    ) -> dict[str, Any]:
        """Synchronous conservation analysis."""
        try:
            # Parse temporal and geographic coverage
            temporal_coverage = {}
            geographic_coverage = set()

            for seq in sequences:
                # Extract year from collection_date
                collection_date = seq.get("collection_date", "")
                if collection_date and len(collection_date) >= 4:
                    try:
                        year = int(collection_date[:4])
                        temporal_coverage[year] = temporal_coverage.get(year, 0) + 1
                    except ValueError:
                        pass

                # Extract geographic information
                country = seq.get("isolation_country", "")
                if country:
                    geographic_coverage.add(country)

            # Mock conservation analysis (in production, this would analyze the alignment)
            mock_conserved_regions = [
                {
                    "region_id": "domain_II_fusion_loop",
                    "start_position": 85,
                    "end_position": 120,
                    "conservation_score": 0.98,
                    "sequence_motif": "DRGWGNGCGLFGKGSL",
                    "functional_annotation": "membrane fusion domain",
                },
                {
                    "region_id": "membrane_proximal_region",
                    "start_position": 380,
                    "end_position": 400,
                    "conservation_score": 0.95,
                    "sequence_motif": "KAWDEDLKYTGNPSL",
                    "functional_annotation": "membrane anchoring",
                },
                {
                    "region_id": "antigenic_site_A",
                    "start_position": 145,
                    "end_position": 165,
                    "conservation_score": 0.92,
                    "sequence_motif": "NGVQNVEELLPKN",
                    "functional_annotation": "immunogenic surface region",
                },
            ]

            # Filter by conservation threshold
            highly_conserved = [
                region
                for region in mock_conserved_regions
                if region["conservation_score"] >= self._conservation_threshold
            ]

            conservation_scores = [
                region["conservation_score"] for region in mock_conserved_regions
            ]

            return {
                "highly_conserved_regions": highly_conserved,
                "conservation_scores": conservation_scores,
                "temporal_coverage": temporal_coverage,
                "geographic_coverage": list(geographic_coverage),
                "total_sequences_analyzed": len(sequences),
                "alignment_length": 450,  # Mock alignment length
                "conservation_method": "position-wise identity scoring",
            }

        finally:
            # Cleanup alignment file
            if os.path.exists(alignment_file):
                os.unlink(alignment_file)
