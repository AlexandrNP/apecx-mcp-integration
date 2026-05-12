#!/usr/bin/env python3
"""
Fixed EEEV workflow test with proper environment setup and execution flow.
"""

import asyncio
import os
import sys
from pathlib import Path

# Add src to Python path
sys.path.insert(0, str(Path(__file__).parent / "src"))


async def test_eeev_fixed_workflow():
    """Test EEEV workflow with comprehensive environment setup."""

    # Set required environment variables
    os.environ["APECX_LLM_BASE_URL"] = "http://localhost:11434/v1"
    os.environ["APECX_LLM_MODEL"] = "mistral-nemo:latest"
    os.environ["APECX_LLM_API_KEY"] = "none"
    os.environ["APECX_LLM_TEMPERATURE"] = "0.7"
    os.environ["APECX_LLM_MAX_TOKENS"] = "2048"

    # Set data paths (use workspace defaults if not set)
    if "APECX_DATA_ROOT" not in os.environ:
        os.environ["APECX_DATA_ROOT"] = str(Path.cwd().parent / "data")

    # Import after environment setup
    from nanobrain.core.workflow import Workflow

    print("🔧 Setting up EEEV workflow...")

    # Load the fixed workflow
    workflow_path = Path("eeev_fixed_workflow.yml")
    workflow = Workflow.from_config(workflow_path)

    print("✅ Workflow loaded successfully")

    # Prepare EEEV epitope query
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
        ],
    }

    print("🔄 Executing EEEV epitope analysis...")
    print(f"Query: {eeev_query['query'][:60]}...")

    # Set input and wait for processing
    await workflow.input_data_units["workflow_input"].set(eeev_query)

    # Wait longer for full retrieval and synthesis
    print("⏳ Waiting for retrieval and synthesis (30s)...")
    await asyncio.sleep(30)

    # Get result
    result = await workflow.output_data_units["workflow_output"].get()

    print("\n" + "=" * 60)
    print("EEEV EPITOPE ANALYSIS RESULTS")
    print("=" * 60)

    if result:
        if isinstance(result, dict):
            # Extract synthesis content
            if "synthesis" in result:
                synthesis = result["synthesis"]
                print(synthesis)
            else:
                print("Raw result structure:")
                for key, value in result.items():
                    print(f"{key}: {type(value)} - {str(value)[:100]}...")
        else:
            print(f"Result type: {type(result)}")
            print(str(result)[:1000] + "..." if len(str(result)) > 1000 else str(result))
    else:
        print("❌ No results returned - checking step execution...")

        # Debug: Check intermediate outputs
        assembly_output = (
            await workflow.child_steps["assembly"]
            .output_data_units["synthesis_bundle_output"]
            .get()
        )
        if assembly_output:
            print(
                f"✅ Assembly step produced: {type(assembly_output)} with keys: {list(assembly_output.keys()) if isinstance(assembly_output, dict) else 'non-dict'}"
            )
        else:
            print("❌ Assembly step produced no output")

    print("=" * 60)
    return result


if __name__ == "__main__":
    result = asyncio.run(test_eeev_fixed_workflow())
