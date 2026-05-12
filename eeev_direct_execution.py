#!/usr/bin/env python3
"""
Direct EEEV workflow execution bypassing trigger binding issues.
"""

import asyncio
import os
import sys
from pathlib import Path

# Add src to Python path
sys.path.insert(0, str(Path(__file__).parent / "src"))


async def execute_eeev_directly():
    """Execute EEEV workflow by directly calling step process methods."""

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

    print("🔧 Creating steps directly...")

    # Create assembly step
    assembly_step = SynthesisContextAssemblyStep.from_config(
        Path("steps/synthesis_context_assembly.yml")
    )
    print("✅ Assembly step created")

    # Create synthesis step
    synthesis_step = RagSynthesisStep.from_config(Path("steps/rag_synthesis.yml"))
    print("✅ Synthesis step created")

    # Prepare EEEV query
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

    print("🔄 Step 1: Running assembly step...")
    print(f"Query: {eeev_query['query'][:60]}...")

    try:
        # Execute assembly step directly
        assembly_result = await assembly_step.process({"assembly_input": eeev_query})
        print(
            f"✅ Assembly completed. Result keys: {list(assembly_result.keys()) if assembly_result else 'None'}"
        )

        if assembly_result and "synthesis_bundle_output" in assembly_result:
            synthesis_bundle = assembly_result["synthesis_bundle_output"]
            print(
                f"Assembly bundle contains: {list(synthesis_bundle.keys()) if isinstance(synthesis_bundle, dict) else 'non-dict'}"
            )

            print("🔄 Step 2: Running synthesis step...")

            # Execute synthesis step directly
            synthesis_result = await synthesis_step.process({"synthesis_input": synthesis_bundle})
            print(
                f"✅ Synthesis completed. Result keys: {list(synthesis_result.keys()) if synthesis_result else 'None'}"
            )

            print("\n" + "=" * 60)
            print("EEEV EPITOPE ANALYSIS RESULTS - DIRECT EXECUTION")
            print("=" * 60)

            if synthesis_result and "synthesis_output" in synthesis_result:
                output = synthesis_result["synthesis_output"]
                if isinstance(output, dict) and "synthesis" in output:
                    print(output["synthesis"])
                else:
                    print(str(output)[:1500] + "..." if len(str(output)) > 1500 else str(output))
            else:
                print("❌ No synthesis output produced")
                print(f"Raw synthesis result: {synthesis_result}")

        else:
            print("❌ Assembly step failed to produce synthesis bundle")
            print(f"Raw assembly result: {assembly_result}")

    except Exception as e:
        print(f"❌ Error during execution: {e}")
        import traceback

        traceback.print_exc()

    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(execute_eeev_directly())
