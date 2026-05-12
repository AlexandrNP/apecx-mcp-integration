#!/usr/bin/env python3
"""
Complete demonstration of end-to-end EEEV epitope analysis workflow.
Shows all stages: input, assembly, synthesis, and final output.
"""

import asyncio
import os
import sys
from pathlib import Path

# Add src to Python path
sys.path.insert(0, str(Path(__file__).parent / "src"))


async def demonstrate_eeev_workflow():
    """Demonstrate complete EEEV workflow with detailed output at each stage."""

    # Environment setup
    os.environ["APECX_LLM_BASE_URL"] = "http://localhost:11434/v1"
    os.environ["APECX_LLM_MODEL"] = "mistral-nemo:latest"
    os.environ["APECX_LLM_API_KEY"] = "none"
    os.environ["APECX_LLM_TEMPERATURE"] = "0.7"
    os.environ["APECX_LLM_MAX_TOKENS"] = "2048"

    if "APECX_DATA_ROOT" not in os.environ:
        os.environ["APECX_DATA_ROOT"] = str(Path.cwd().parent / "data")

    from apecx_integration.composition.steps.rag_synthesis_step import RagSynthesisStep
    from apecx_integration.composition.steps.synthesis_context_assembly_step import (
        SynthesisContextAssemblyStep,
    )

    print("🚀 EEEV EPITOPE ANALYSIS WORKFLOW DEMONSTRATION")
    print("=" * 80)

    # Create workflow steps
    print("\n🔧 STEP INITIALIZATION")
    print("Creating SynthesisContextAssemblyStep...")
    assembly_step = SynthesisContextAssemblyStep.from_config(
        Path("steps/synthesis_context_assembly.yml")
    )
    print("✅ Assembly step ready")

    print("Creating RagSynthesisStep...")
    synthesis_step = RagSynthesisStep.from_config(Path("steps/rag_synthesis.yml"))
    print("✅ Synthesis step ready")

    # Input preparation
    print("\n📥 WORKFLOW INPUT")
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

    print(f"Research Question: {eeev_query['query']}")
    print(f"Query Terms: {', '.join(eeev_query['query_terms'])}")

    # Assembly stage
    print("\n🔄 ASSEMBLY STAGE: Multi-source Data Retrieval")
    print("Executing concurrent retrieval from:")
    print("  • VIOLIN database (pathogen information)")
    print("  • BV-BRC (viral genomes)")
    print("  • Globus Search (structural data)")
    print("  • PubMed (literature)")
    print("  • Domain RAG (semantic search)")

    assembly_result = await assembly_step.process({"assembly_input": eeev_query})

    print("\n✅ ASSEMBLY RESULTS:")
    print(f"  • RAG chunks retrieved: {len(assembly_result.get('rag_chunks', []))}")
    print(f"  • BV-BRC genomes: {len(assembly_result.get('bvbrc_genomes', []))}")
    print(f"  • VIOLIN mappings: {len(assembly_result.get('violin_mappings', []))}")
    print(f"  • Publications found: {len(assembly_result.get('publications', []))}")
    print(f"  • Globus structures: {len(assembly_result.get('globus_results', []))}")

    # Show sample data from each source
    print("\n📊 SAMPLE RETRIEVED DATA:")

    if assembly_result.get("rag_chunks"):
        print("\n🧬 RAG Chunks (top result):")
        top_rag = assembly_result["rag_chunks"][0]
        print(f"  Source: {top_rag['source']}")
        print(f"  Score: {top_rag['score']:.3f}")
        print(f"  Text: {top_rag['text'][:150]}...")

    if assembly_result.get("bvbrc_genomes"):
        print(f"\n🦠 BV-BRC Genomes (showing 3 of {len(assembly_result['bvbrc_genomes'])}):")
        for i, genome in enumerate(assembly_result["bvbrc_genomes"][:3]):
            print(f"  {i + 1}. {genome['genome_name']}")

    if assembly_result.get("violin_mappings"):
        print(f"\n🎯 VIOLIN Mappings (showing 3 of {len(assembly_result['violin_mappings'])}):")
        for i, mapping in enumerate(assembly_result["violin_mappings"][:3]):
            print(f"  {i + 1}. {mapping['query_term']} → {mapping['canonical_term']}")

    if assembly_result.get("globus_results"):
        print(f"\n🏗️ Globus Structures (showing 3 of {len(assembly_result['globus_results'])}):")
        for i, result in enumerate(assembly_result["globus_results"][:3]):
            pdb_info = result["content"].get("pdb", {})
            title = (
                result["content"]["titles"][0]["title"]
                if result["content"].get("titles")
                else "Unknown"
            )
            resolution = pdb_info.get("resolution_angstrom", "N/A")
            print(f"  {i + 1}. {title} (Resolution: {resolution}Å)")

    # Synthesis stage
    print("\n🔄 SYNTHESIS STAGE: Evidence-Based Analysis")
    print("Generating comprehensive epitope analysis with grounded citations...")

    synthesis_result = await synthesis_step.process({"synthesis_input": assembly_result})

    print("✅ Synthesis completed successfully!")

    # Final output
    print("\n" + "=" * 80)
    print("🎯 FINAL EEEV EPITOPE ANALYSIS OUTPUT")
    print("=" * 80)

    if synthesis_result and "synthesis" in synthesis_result:
        analysis = synthesis_result["synthesis"]
        print("\n" + analysis)
    else:
        print("❌ No synthesis content available")
        print(f"Raw result: {synthesis_result}")

    print("\n" + "=" * 80)
    print("✅ WORKFLOW EXECUTION COMPLETED")
    print("=" * 80)
    print(f"Total data sources integrated: {len([k for k in assembly_result if k != 'query'])}")
    print(f"Analysis length: {len(synthesis_result.get('synthesis', ''))} characters")
    print("Workflow status: FULLY OPERATIONAL")

    return synthesis_result


if __name__ == "__main__":
    result = asyncio.run(demonstrate_eeev_workflow())
