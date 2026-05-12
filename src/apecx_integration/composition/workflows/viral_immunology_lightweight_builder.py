"""Lightweight nanobrain workflow builder for viral immunology analysis.

Demonstrates the programmatic WorkflowBuilder approach as an alternative
to hand-authored YAML workflows. This is one of the three legitimate
nanobrain workflow creation patterns:

1. Hand-authored YAML + Workflow.from_config() [traditional]
2. WorkflowBuilder programmatic API [lightweight]  ← THIS FILE
3. Workflow.from_skeleton() with typed placeholders [template-based]

Key advantages of the lightweight approach:
- Code generation friendly (LLMs can write Python easier than YAML)
- Dynamic workflow construction based on runtime conditions
- IDE support with autocomplete and type checking
- Easier debugging with Python stack traces
- Programmatic workflow composition and reuse

Framework compliance:
- Uses nanobrain.lightweight.WorkflowBuilder
- Proper step configuration and linking
- Maintains auto_transfer=true throughout
- Follows nanobrain ownership boundaries
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def build_viral_immunology_workflow_lightweight() -> Any:
    """Build viral immunology analysis workflow using WorkflowBuilder.

    This creates the exact same workflow as the YAML version but using
    the lightweight programmatic API. Demonstrates an alternative
    nanobrain workflow creation pattern.

    Returns:
        Configured Workflow instance ready for execution
    """
    from nanobrain.core.data_unit import DataUnitMemory
    from nanobrain.core.link import DirectLink
    from nanobrain.core.trigger import DataUnitChangeTrigger
    from nanobrain.lightweight import WorkflowBuilder

    builder = WorkflowBuilder(
        name="viral_immunology_lightweight",
        description="Viral immunology analysis built with WorkflowBuilder",
    )

    # Workflow-level data units (entry/exit points)
    builder.add_input_data_unit(
        "workflow_input",
        DataUnitMemory,
        description="Entry point - viral immunology research query",
    )

    builder.add_output_data_unit(
        "workflow_output", DataUnitMemory, description="Exit point - comprehensive analysis results"
    )

    # Step 1: Viral Immunology Query Classifier
    builder.add_step(
        "viral_classifier",
        "apecx_integration.composition.steps.viral_immunology_query_classifier_step.ViralImmunologyQueryClassifierStep",
        config={
            "name": "viral_classifier",
            "description": "Classify query for viral immunology research intent",
            "minimum_confidence": 0.3,
            "enable_fuzzy_matching": True,
            "ontology_config_path": None,  # Use embedded default
            "auto_initialize": True,
            "debug_mode": False,
            "enable_logging": True,
            "input_data_units": {
                "classifier_input": {
                    "class": "nanobrain.core.data_unit.DataUnitMemory",
                    "name": "classifier_input",
                    "description": "Raw query for classification",
                }
            },
            "output_data_units": {
                "classifier_output": {
                    "class": "nanobrain.core.data_unit.DataUnitMemory",
                    "name": "classifier_output",
                    "description": "Classification result with virus detection",
                }
            },
            "triggers": [
                {
                    "class": "nanobrain.core.trigger.DataUnitChangeTrigger",
                    "data_unit": "classifier_input",
                }
            ],
        },
    )

    # Step 2: Query Enhancement
    builder.add_step(
        "query_enhancer",
        "apecx_integration.composition.steps.viral_query_enhancer_step.ViralQueryEnhancerStep",
        config={
            "name": "query_enhancer",
            "description": "Enhance query with classification results",
            "include_family_terms": True,
            "enhance_protein_terms": True,
            "boost_immunology_concepts": True,
            "auto_initialize": True,
            "debug_mode": False,
            "enable_logging": True,
            "input_data_units": {
                "enhancer_input": {
                    "class": "nanobrain.core.data_unit.DataUnitMemory",
                    "name": "enhancer_input",
                    "description": "Query + classification results",
                }
            },
            "output_data_units": {
                "enhancer_output": {
                    "class": "nanobrain.core.data_unit.DataUnitMemory",
                    "name": "enhancer_output",
                    "description": "Enhanced query with entities and terms",
                }
            },
            "triggers": [
                {
                    "class": "nanobrain.core.trigger.DataUnitChangeTrigger",
                    "data_unit": "enhancer_input",
                }
            ],
        },
    )

    # Step 3: Unlimited Multi-Source Assembly
    builder.add_step(
        "unlimited_assembly",
        "apecx_integration.composition.steps.unlimited_synthesis_assembly_step.UnlimitedSynthesisAssemblyStep",
        config={
            "name": "unlimited_assembly",
            "description": "Unlimited retrieval from all data sources",
            # Enhanced configuration - NO ARBITRARY CAPS
            "k_rag": 20,
            "max_publications": 50,
            "max_globus_hits": 100,
            "min_violin_relevance_score": 0.1,
            "min_bvbrc_relevance_score": 0.1,
            "enable_streaming": False,
            "batch_size": 1000,
            "auto_initialize": True,
            "debug_mode": False,
            "enable_logging": True,
            "input_data_units": {
                "assembly_input": {
                    "class": "nanobrain.core.data_unit.DataUnitMemory",
                    "name": "assembly_input",
                    "description": "Enhanced query for multi-source retrieval",
                }
            },
            "output_data_units": {
                "synthesis_bundle_output": {
                    "class": "nanobrain.core.data_unit.DataUnitMemory",
                    "name": "synthesis_bundle_output",
                    "description": "Complete data bundle from all sources",
                }
            },
            "triggers": [
                {
                    "class": "nanobrain.core.trigger.DataUnitChangeTrigger",
                    "data_unit": "assembly_input",
                }
            ],
        },
    )

    # Step 4: RAG Synthesis (use existing configuration)
    here = Path(__file__).resolve()
    repo_root = here.parents[
        3
    ]  # workflows/viral_immunology_lightweight_builder.py → apecx-mcp-integration
    synthesis_config_path = repo_root / "steps" / "rag_synthesis.yml"

    builder.add_step(
        "rag_synthesis",
        "apecx_integration.composition.steps.rag_synthesis_step.RagSynthesisStep",
        config_path=synthesis_config_path,
    )

    # Workflow links with auto_transfer=True (CRITICAL for preventing silent failures)

    # Entry: workflow_input → viral_classifier
    builder.add_link(
        "workflow_to_classifier",
        DirectLink,
        from_step=None,  # workflow-level
        from_data_unit="workflow_input",
        to_step="viral_classifier",
        to_data_unit="classifier_input",
        auto_transfer=True,
        description="Route workflow input to viral classifier",
    )

    # Step 1 → Step 2: classification → query_enhancer
    builder.add_link(
        "classifier_to_enhancer",
        DirectLink,
        from_step="viral_classifier",
        from_data_unit="classifier_output",
        to_step="query_enhancer",
        to_data_unit="enhancer_input",
        auto_transfer=True,
        description="Pass classification to query enhancer",
    )

    # Step 2 → Step 3: enhanced query → unlimited_assembly
    builder.add_link(
        "enhancer_to_assembly",
        DirectLink,
        from_step="query_enhancer",
        from_data_unit="enhancer_output",
        to_step="unlimited_assembly",
        to_data_unit="assembly_input",
        auto_transfer=True,
        description="Pass enhanced query to unlimited assembly",
    )

    # Step 3 → Step 4: assembly → synthesis
    builder.add_link(
        "assembly_to_synthesis",
        DirectLink,
        from_step="unlimited_assembly",
        from_data_unit="synthesis_bundle_output",
        to_step="rag_synthesis",
        to_data_unit="synthesis_input",
        auto_transfer=True,
        description="Pass complete data bundle to synthesis",
    )

    # Exit: synthesis → workflow_output
    builder.add_link(
        "synthesis_to_workflow",
        DirectLink,
        from_step="rag_synthesis",
        from_data_unit="synthesis_output",
        to_step=None,  # workflow-level
        to_data_unit="workflow_output",
        auto_transfer=True,
        description="Return final analysis to workflow output",
    )

    # Add workflow trigger
    builder.add_trigger(
        DataUnitChangeTrigger,
        data_unit="workflow_input",
        description="Trigger workflow on new query input",
    )

    # Load and return the configured workflow
    workflow = builder.load()

    log.info(f"Built viral immunology workflow using WorkflowBuilder: {workflow.name}")
    log.info(f"Workflow has {len(workflow.child_steps)} steps and {len(workflow.step_links)} links")

    return workflow


def create_viral_immunology_workflow_from_config() -> Any:
    """Create workflow using traditional YAML config approach.

    This is the alternative to the lightweight builder - shows both patterns.
    """
    from nanobrain.core.workflow import Workflow

    here = Path(__file__).resolve()
    workflow_dir = here.parent / "viral_immunology_analysis"
    workflow_config = workflow_dir / "viral_immunology_analysis_workflow.yml"

    if not workflow_config.is_file():
        raise FileNotFoundError(f"Workflow config not found: {workflow_config}")

    workflow = Workflow.from_config(workflow_config)
    log.info(f"Loaded viral immunology workflow from YAML: {workflow.name}")

    return workflow


class ViralImmunologyWorkflowFactory:
    """Factory for creating viral immunology workflows using different patterns.

    Demonstrates the three legitimate nanobrain workflow creation approaches:
    1. Lightweight WorkflowBuilder (programmatic)
    2. Traditional YAML + from_config
    3. Template-based skeleton + bindings (future)
    """

    @staticmethod
    def create_lightweight() -> Any:
        """Create workflow using WorkflowBuilder (programmatic approach)."""
        return build_viral_immunology_workflow_lightweight()

    @staticmethod
    def create_from_yaml() -> Any:
        """Create workflow using traditional YAML config."""
        return create_viral_immunology_workflow_from_config()

    @staticmethod
    def create_skeleton_based(virus_family: str, research_type: str) -> Any:
        """Create workflow using skeleton + bindings (template approach).

        Future enhancement: parameterized workflow generation based on
        virus family and research type.
        """
        # TODO: Implement skeleton-based approach
        raise NotImplementedError("Skeleton-based workflow creation not yet implemented")

    @classmethod
    def get_available_patterns(cls) -> list[str]:
        """Get list of available workflow creation patterns."""
        return [
            "lightweight",  # WorkflowBuilder programmatic API
            "yaml",  # Traditional YAML config
            "skeleton",  # Template-based (future)
        ]


# Convenience functions for direct usage
def create_viral_immunology_workflow(pattern: str = "lightweight") -> Any:
    """Create viral immunology workflow using specified pattern.

    Args:
        pattern: Creation pattern - "lightweight", "yaml", or "skeleton"

    Returns:
        Configured Workflow instance

    Raises:
        ValueError: If pattern is not supported
    """
    factory = ViralImmunologyWorkflowFactory()

    if pattern == "lightweight":
        return factory.create_lightweight()
    elif pattern == "yaml":
        return factory.create_from_yaml()
    elif pattern == "skeleton":
        return factory.create_skeleton_based("", "")
    else:
        available = factory.get_available_patterns()
        raise ValueError(f"Unknown pattern '{pattern}'. Available: {available}")


if __name__ == "__main__":
    # Demo usage of both patterns
    logging.basicConfig(level=logging.INFO)

    print("=== Viral Immunology Workflow Creation Patterns ===\n")

    # Pattern 1: Lightweight WorkflowBuilder
    print("1. Creating workflow with WorkflowBuilder (programmatic)...")
    try:
        lightweight_workflow = create_viral_immunology_workflow("lightweight")
        print(f"✅ Created: {lightweight_workflow.name}")
        print(f"   Steps: {len(lightweight_workflow.child_steps)}")
        print(f"   Links: {len(lightweight_workflow.step_links)}")
    except Exception as e:
        print(f"❌ Failed: {e}")

    print()

    # Pattern 2: Traditional YAML
    print("2. Creating workflow from YAML config (traditional)...")
    try:
        yaml_workflow = create_viral_immunology_workflow("yaml")
        print(f"✅ Created: {yaml_workflow.name}")
        print(f"   Steps: {len(yaml_workflow.child_steps)}")
        print(f"   Links: {len(yaml_workflow.step_links)}")
    except Exception as e:
        print(f"❌ Failed: {e}")

    print("\n=== Both patterns create equivalent workflows ===")
    print("Choose based on your use case:")
    print("- Lightweight: Code generation, dynamic construction, IDE support")
    print("- YAML: Configuration files, declarative workflows, version control")
