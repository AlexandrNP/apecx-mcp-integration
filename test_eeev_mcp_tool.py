#!/usr/bin/env python3
"""
Test the EEEV MCP tool integration to ensure it works correctly.
"""

import asyncio
import sys
from pathlib import Path

# Add src to Python path
sys.path.insert(0, str(Path(__file__).parent / "src"))


async def test_eeev_mcp_tool():
    """Test the EEEV MCP tool functionality."""

    from apecx_integration.mcp_surface.tools.eeev_epitope_analysis import (
        _is_eeev_query,
        analyze_eeev_epitopes,
    )

    print("🧪 TESTING EEEV MCP TOOL")
    print("=" * 50)

    # Test query detection
    print("\n🔍 Testing query detection:")

    test_queries = [
        "Identify EEEV epitopes on envelope glycoprotein",
        "What are neutralizing antibodies for Eastern Equine Encephalitis?",
        "Find epitopes conserved across EEEV strains",
        "Tell me about COVID vaccine development",  # Should be False
        "EEEV structural analysis and immunogenic regions",
    ]

    for query in test_queries:
        is_eeev = _is_eeev_query(query)
        status = "✅ EEEV" if is_eeev else "❌ Not EEEV"
        print(f"  {status}: {query[:60]}...")

    # Test EEEV analysis
    print("\n🔄 Testing EEEV epitope analysis:")

    eeev_query = "Identify neutralizing epitopes on Eastern Equine Encephalitis Virus envelope glycoprotein that are conserved across viral strains and accessible for antibody binding"

    print(f"Query: {eeev_query}")
    print("Executing analysis...")

    try:
        result = await analyze_eeev_epitopes(eeev_query, skip_pubmed=True)

        if "error" in result:
            print(f"❌ Error: {result['error']}")
        else:
            print("✅ Analysis completed successfully!")
            print(f"  - Query type: {result.get('query_type', 'unknown')}")
            print(f"  - Epitopes found: {result.get('epitopes_found', 0)}")

            data_sources = result.get("data_sources", {})
            print("  - Data sources integrated:")
            for source, count in data_sources.items():
                print(f"    • {source}: {count}")

            analysis = result.get("analysis", "")
            if analysis:
                print(f"  - Analysis length: {len(analysis)} characters")
                print(f"  - Analysis preview: {analysis[:200]}...")
            else:
                print("  - No analysis content returned")

    except Exception as e:
        print(f"❌ Exception: {e}")
        import traceback

        traceback.print_exc()

    # Test non-EEEV query rejection
    print("\n🚫 Testing non-EEEV query rejection:")

    non_eeev_query = "What are the symptoms of influenza?"
    try:
        result = await analyze_eeev_epitopes(non_eeev_query)
        if "error" in result:
            print(f"✅ Correctly rejected: {result['error']}")
        else:
            print("❌ Should have rejected non-EEEV query")
    except Exception as e:
        print(f"❌ Unexpected error: {e}")

    print("\n" + "=" * 50)
    print("🏁 EEEV MCP TOOL TEST COMPLETED")


if __name__ == "__main__":
    asyncio.run(test_eeev_mcp_tool())
