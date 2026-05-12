#!/usr/bin/env python3
"""
Complete EEEV workflow execution with proper data formatting.
"""

import asyncio
import os
import sys
from pathlib import Path

# Add src to Python path
sys.path.insert(0, str(Path(__file__).parent / "src"))


async def execute_eeev_complete():
    """Execute EEEV workflow with proper data formatting between steps."""

    # Set environment variables
    os.environ["APECX_LLM_BASE_URL"] = "http://localhost:11434/v1"
    os.environ["APECX_LLM_MODEL"] = "mistral-nemo:latest"
    os.environ["APECX_LLM_API_KEY"] = "none"
    os.environ["APECX_LLM_TEMPERATURE"] = "0.7"
    os.environ["APECX_LLM_MAX_TOKENS"] = "2048"

    if "APECX_DATA_ROOT" not in os.environ:
        os.environ["APECX_DATA_ROOT"] = str(Path.cwd().parent / "data")

    # Import step classes directly
    from apecx_integration.composition.steps.rag_synthesis_step import RagSynthesisStep
    from apecx_integration.composition.steps.synthesis_context_assembly_step import (
        SynthesisContextAssemblyStep,
    )

    print("🔧 Creating EEEV workflow steps...")

    # Create steps
    assembly_step = SynthesisContextAssemblyStep.from_config(
        Path("steps/synthesis_context_assembly.yml")
    )
    synthesis_step = RagSynthesisStep.from_config(Path("steps/rag_synthesis.yml"))

    # EEEV query
    eeev_query = {
        "query": "Identify neutralizing epitopes on Eastern Equine Encephalitis Virus envelope glycoprotein that are conserved across viral strains and accessible for antibody binding",
        "entities": None,
        "query_terms": [
            "neutralizing",
            "epitopes",
            "EEEV",
            "envelope",
            "glycoprotein",
            "conserved",
            "antibody",
            "binding",
        ],
    }

    print("🔄 Step 1: Assembly - Multi-source data retrieval...")
    print(f"Query: {eeev_query['query'][:80]}...")

    # Execute assembly step
    assembly_result = await assembly_step.process({"assembly_input": eeev_query})

    print(f"✅ Assembly completed with {len(assembly_result)} data streams")
    print(f"- RAG chunks: {len(assembly_result.get('rag_chunks', []))}")
    print(f"- BV-BRC genomes: {len(assembly_result.get('bvbrc_genomes', []))}")
    print(f"- VIOLIN mappings: {len(assembly_result.get('violin_mappings', []))}")
    print(f"- Publications: {len(assembly_result.get('publications', []))}")
    print(f"- Globus structures: {len(assembly_result.get('globus_results', []))}")

    # The assembly step returns the synthesis bundle directly as expected by RagSynthesisStep
    synthesis_bundle = assembly_result

    print("🔄 Step 2: Synthesis - Generating comprehensive epitope analysis...")

    try:
        # Execute synthesis step with the assembled data
        synthesis_result = await synthesis_step.process({"synthesis_input": synthesis_bundle})

        print("✅ Synthesis completed successfully!")

        print("\n" + "=" * 80)
        print("EEEV EPITOPE ANALYSIS RESULTS")
        print("=" * 80)

        if synthesis_result and "synthesis_output" in synthesis_result:
            output_data = synthesis_result["synthesis_output"]

            # Extract the actual synthesis content
            if isinstance(output_data, dict) and "synthesis" in output_data:
                synthesis_content = output_data["synthesis"]
                print(synthesis_content)
            else:
                # If the output structure is different, display what we have
                print(
                    str(output_data)[:3000] + "..."
                    if len(str(output_data)) > 3000
                    else str(output_data)
                )
        else:
            print("❌ No synthesis output produced")
            print(f"Raw synthesis result: {synthesis_result}")

        print("=" * 80)
        print("WORKFLOW EXECUTION COMPLETED SUCCESSFULLY")
        print("=" * 80)

        return synthesis_result

    except Exception as e:
        print(f"❌ Error during synthesis: {e}")
        import traceback

        traceback.print_exc()
        return None


if __name__ == "__main__":
    result = asyncio.run(execute_eeev_complete())
