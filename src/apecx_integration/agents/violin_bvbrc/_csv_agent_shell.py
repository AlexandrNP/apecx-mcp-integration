"""Dormant CSV-agent paths extracted from violin_bvbrc/agent.py (G79).

This module owns every code path that depends on
``langchain_experimental.agents.agent_toolkits.create_csv_agent`` —
the LangChain 1.x-incompatible CSV agent shell that used to drive
``process_query`` / ``process_bvbrc_query``. None of the live
workflow paths (entity extraction → synonym search → enrichment)
touch any of this; the only consumers are interactive ``main()`` and
the dormant ``process_query`` end-to-end orchestrator.

Why this lives in a separate file (G79)
========================================

1. **Size.** Lines 543-863 + 1156-1424 of the pre-G79 agent.py
   (~600 lines) were all CSV-agent-shell code. Splitting them out
   shrinks agent.py to ~825 lines and gives the live LLM-driven
   helpers (entity extraction, rank-terms, synonym search) room to
   breathe.

2. **Dependency isolation.** The CSV-agent paths inline-import
   ``langchain.agents.agent_types.AgentType``,
   ``langchain_experimental.agents.agent_toolkits.create_csv_agent``,
   and ``langchain_experimental.tools.python.tool.PythonREPLTool``
   because langchain 1.x moved those symbols and
   langchain_experimental is an optional dep. Keeping the failure
   at call time means importers that only want the live entity-
   extraction path don't pay an import-time cost.

3. **Failure-mode quarantine.** When a CSV-agent path breaks
   (model doesn't honor OPENAI_FUNCTIONS schema, langchain
   reorganises a submodule), the breakage is localized — agent.py's
   live paths cannot become collateral damage.

Public re-exports
=================

agent.py re-imports every symbol below at the end of its module
body, so ``from apecx_integration.agents.violin_bvbrc.agent import X``
still works for every X. New callers should import from this module
directly — the agent.py re-exports exist only for backward
compatibility with pre-G79 imports.
"""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

# Late-binding imports from agent.py — agent.py is the partially-
# executed parent module when this file is first imported (the
# import sequence is: caller → agent.py runs all definitions →
# agent.py imports this module at the end). By that point every
# symbol below is already defined on agent.py, so this import
# resolves cleanly.
from apecx_integration.agents.violin_bvbrc.agent import (
    CSV_FILES,
    _build_chat_llm,
    _get_dfs,
    consolidated_synonym_search,
    extract_entities_llm,
    get_agent_statistics,
)

# Create a structured system prompt with all the context about the VIOLIN database
SYSTEM_PROMPT = """
You are an expert assistant for the VIOLIN (Vaccine Investigation and Online Information Network) database and BV-BRC (Bacterial and Viral Bioinformatics Resource Center).
Your task is to answer questions about vaccines, pathogens, genes, genomes, and their relationships.

The system uses two separate agents to process queries:
1. VIOLIN Agent: Processes information about vaccines, pathogens, and genes
2. BV-BRC Agent: Processes genomic information for alphaviruses

The VIOLIN database consists of the following tables:
1. Vaccine_Information.csv: Contains details about vaccines
2. Pathogen_Information.csv: Contains information about pathogens
3. Gene_Information.csv: Contains information about genes
4. Gene_Vaccine_Pathogen_Information.csv: Maps relationships between genes, vaccines, and pathogens
5. Vaccine_Pathogen_Information.csv: Maps relationships between vaccines and pathogens

The BV-BRC database consists of:
- BVBRC_genome_alphavirus.csv: Contains genomic information for alphaviruses

When answering:
1. The system will automatically identify which database(s) to query based on the entities in your question
2. Queries about vaccines, pathogens, and genes will be directed to the VIOLIN database
3. Queries about alphavirus genomes will be directed to the BV-BRC database
4. If your query mentions pathogens, the system will also search for relevant genomes in the BV-BRC database
5. If your query contains both types of entities, both databases will be queried and results combined
6. Format your answer clearly with proper headings and structure
7. Always cite the source tables used in your answer

Your answers will include comprehensive, accurate information from both databases when appropriate.
"""


def initialize_csv_agent(verbose: bool = False) -> Any:
    """Initialize a CSV agent with access to all VIOLIN database tables.

    Note: CSV agents use LangChain's ``AgentType.OPENAI_FUNCTIONS`` which
    depends on OpenAI-style function calling. Local models' support for
    this is uneven — mistral-small:24b may honor the schema, mistral-nemo
    may not. Verify quality against the specific model before relying on
    this path. Step 1 / 3c / 5 do NOT use these CSV agents, so the path
    is currently dormant in the violin_bvbrc workflow.
    """
    # Lazy imports: langchain 1.x moved these symbols and langchain_experimental
    # is an optional dep. Keep the failure at call time so the module stays
    # importable for the three public, LLM-light entry points.
    from langchain.agents.agent_types import AgentType
    from langchain_experimental.agents.agent_toolkits import create_csv_agent
    from langchain_experimental.tools.python.tool import PythonREPLTool

    llm = _build_chat_llm(
        temperature=0,
        max_tokens=16384,
        request_timeout=600,
    )

    # Create the CSV agent with all CSV files except BVBRC and Python REPL capability for data manipulation
    violin_csv_files = [
        CSV_FILES["vaccines"],
        CSV_FILES["pathogens"],
        CSV_FILES["genes"],
        CSV_FILES["gene_vaccine_pathogen"],
        CSV_FILES["vaccine_pathogen"],
    ]

    agent = create_csv_agent(
        llm,
        violin_csv_files,
        agent_type=AgentType.OPENAI_FUNCTIONS,
        verbose=verbose,
        allow_dangerous_code=True,
        max_iterations=1000,
        max_execution_time=600,
        extra_tools=[PythonREPLTool()],
    )

    return agent


def initialize_bvbrc_agent(verbose: bool = False) -> Any:
    """Initialize a CSV agent specifically for the BV-BRC genome data.

    Same function-calling caveat as ``initialize_csv_agent`` — see that
    docstring. Dormant in the violin_bvbrc workflow.
    """
    # Lazy imports — see ``initialize_csv_agent`` for why these stay
    # inline rather than at module top.
    from langchain.agents.agent_types import AgentType
    from langchain_experimental.agents.agent_toolkits import create_csv_agent
    from langchain_experimental.tools.python.tool import PythonREPLTool

    llm = _build_chat_llm(
        temperature=0,
        max_tokens=16384,
        request_timeout=600,
    )

    # System message for the BV-BRC agent
    system_message = """
    You are an expert assistant for analyzing genomic data from the BV-BRC (Bacterial and Viral Bioinformatics Resource Center).
    Your expertise is in alphavirus genomes. You have access to the BVBRC_genome_alphavirus.csv file that contains detailed
    genomic information about alphaviruses.

    When responding to queries:
    1. Focus on providing genomic information and characteristics
    2. Include taxonomy information (Family, Genus, Species, Strain)
    3. Include geographic and host information when available
    4. Provide genome size, GC content, and other genomic features when relevant
    5. When asked about a specific pathogen, provide information about all relevant genomes
       associated with that pathogen
    6. Look for matches in the taxonomy hierarchy (e.g., if asked about "Alphavirus", provide
       information about all alphavirus genomes in the database)
    7. Format your response clearly with headings and structure
    8. Cite specific genome IDs and accession numbers when available

    Your answers should be comprehensive, accurate, and focused on the genomic aspects of the alphaviruses.
    """

    # Create the CSV agent with only the BVBRC genome file
    bvbrc_csv_file = [CSV_FILES["bvbrc_genomes"]]

    agent = create_csv_agent(
        llm,
        bvbrc_csv_file,
        agent_type=AgentType.OPENAI_FUNCTIONS,
        verbose=verbose,
        allow_dangerous_code=True,
        max_iterations=1000,
        max_execution_time=600,
        extra_tools=[PythonREPLTool()],
        system_message=system_message,
    )

    return agent


def post_process_response(response: str, entity_data: dict[str, Any] = None) -> str:
    """Post-process the agent's response to enhance readability and add context."""

    # Helper function to add formatted data fields to the response
    def add_data_fields(response_text, data, priority_fields):
        """Add formatted data fields to the response text."""
        result = response_text
        # First add the priority fields in the specified order
        for field in priority_fields:
            if field in data and data[field]:
                result += f"- {field}: {data[field]}\n"

        # Then add any remaining fields
        for field, value in data.items():
            if field not in priority_fields and value:
                result += f"- {field}: {value}\n"

        return result

    # If the response already contains a source citation, don't add another one
    if "Source:" not in response:
        response += "\n\nSource: VIOLIN Database Tables"

    # Enhance the response with entity data if provided
    if entity_data and "matches" in entity_data and entity_data["matches"]:
        response += "\n\n---\n\nDetailed Database Information:\n"

        # Group matches by entity type for organized display
        grouped_matches = {}
        for match in entity_data["matches"]:
            if "relevant_data" in match:
                query_entity = match["query_entity"]
                entity_type = next(
                    (
                        entity["type"]
                        for entity in extract_entities_llm(query_entity)
                        if entity["confidence"] >= 0.7
                    ),
                    "unknown",
                )

                if entity_type not in grouped_matches:
                    grouped_matches[entity_type] = []
                grouped_matches[entity_type].append(match)

        # Generate detailed information sections by entity type
        for entity_type, matches in grouped_matches.items():
            if entity_type == "pathogen":
                response += "\n## Pathogen Information\n"
            elif entity_type == "vaccine":
                response += "\n## Vaccine Information\n"
            elif entity_type == "gene":
                response += "\n## Gene Information\n"
            elif entity_type == "genome":
                response += "\n## Genome Information\n"
            else:
                response += f"\n## {entity_type.capitalize()} Information\n"

            # Add information for each match in this group
            for match in matches:
                entity_name = match["query_entity"]
                response += f"\n### {entity_name} ({match['synonym']})\n"

                # Format and add the relevant data
                if "relevant_data" in match:
                    for table, data in match["relevant_data"].items():
                        if table == "pathogens":
                            response += "**VIOLIN Pathogen Data:**\n"
                            priority_fields = [
                                "VIOLIN_c_pathogen_id",
                                "Pathogen",
                                "NCBI_Taxonomy_ID",
                                "Disease",
                            ]
                            response = add_data_fields(response, data, priority_fields)

                        elif table == "vaccines":
                            response += "**VIOLIN Vaccine Data:**\n"
                            priority_fields = [
                                "Vaccine",
                                "Vaccine_Name",
                                "Type",
                                "Status",
                                "Antigen",
                            ]
                            response = add_data_fields(response, data, priority_fields)

                        elif table == "genes":
                            response += "**VIOLIN Gene Data:**\n"
                            priority_fields = [
                                "Gene_Name",
                                "Organism",
                                "VIOLIN_c_gene_id",
                                "Protein_Name",
                                "Molecule_Role",
                            ]
                            response = add_data_fields(response, data, priority_fields)

                        elif table == "bvbrc_genomes":
                            response += "**BV-BRC Genome Data:**\n"
                            priority_fields = [
                                "Genome ID",
                                "Genome Name",
                                "Genus",
                                "Species",
                                "Strain",
                                "GenBank Accessions",
                                "Size",
                                "GC Content",
                                "Host Name",
                                "Geographic Location",
                                "Collection Year",
                            ]
                            response = add_data_fields(response, data, priority_fields)

                        elif table == "related_genes":
                            response += "**Related Genes:**\n"
                            for gene_data in data:
                                response += f"- {gene_data.get('Gene_Name', 'Unknown Gene')}"
                                if "Protein_Name" in gene_data:
                                    response += f" ({gene_data['Protein_Name']})"
                                if "Molecule_Role" in gene_data:
                                    response += f": {gene_data['Molecule_Role']}"
                                response += "\n"

    # Annotate field names in the response to improve readability
    field_pattern = r"\b(Name|Type|Status|Description|Efficacy|Disease|Pathogen|Species|Genus|Family|Date|Country|Function|Role|Host|Immunity|Pathogenesis|Genome)\b"
    response = re.sub(field_pattern, r'"\1"', response)

    return response


def process_query(user_query: str, verbose: bool = False) -> str:
    """Process a natural language query against the VIOLIN and BV-BRC databases."""
    # Extract entities to determine if we need to query BV-BRC database
    entities = extract_entities_llm(user_query)

    # Separate entities by type
    genome_entities = [entity for entity in entities if entity["type"] == "genome"]
    pathogen_entities = [entity for entity in entities if entity["type"] == "pathogen"]

    # Process VIOLIN database query (always run this)
    violin_agent = initialize_csv_agent(verbose=verbose)

    # Use the consolidated synonym search to find all entity matches with relevant data
    # Only search in VIOLIN tables for non-genome entities
    violin_dfs = {k: df for k, df in _get_dfs().items() if k != "bvbrc_genomes"}
    synonym_matches = consolidated_synonym_search(
        user_query, dfs=violin_dfs, include_relevant_data=True
    )

    # Build an enhanced query with the consolidated matches and relevant data
    enhanced_query = user_query

    # Store entity data for including in final response
    entity_data = {"matches": []}

    if synonym_matches:
        enhanced_query += "\n\nDetected entities and their matches in the VIOLIN database:"

        # Group results by query entity
        entity_matches = {}
        for match in synonym_matches:
            query_entity = match["query_entity"]
            if query_entity not in entity_matches:
                entity_matches[query_entity] = []
            entity_matches[query_entity].append(match)

        # Add each entity's matches to the enhanced query
        for query_entity, matches in entity_matches.items():
            # Sort matches by score in descending order
            sorted_matches = sorted(matches, key=lambda x: x.get("score", 0), reverse=True)

            enhanced_query += f"\n\n- Entity: '{query_entity}'"
            enhanced_query += "\n  Top matches in database:"

            # Add the top 3 matches with relevant data if available
            for i, match in enumerate(sorted_matches[:3], 1):
                synonym = match["synonym"]
                score = match["score"]
                enhanced_query += f"\n  {i}. '{synonym}' (Similarity: {score:.2f})"

                # Add relevant data if available
                if "relevant_data" in match:
                    relevant_data = match["relevant_data"]
                    if relevant_data:
                        enhanced_query += "\n     Key information:"

                        # Also store for including in final response
                        entity_data["matches"].append(match)

                        # Add main entity information
                        for table_name, data in relevant_data.items():
                            if table_name != "related_genes":
                                enhanced_query += f"\n     Table: {table_name}"
                                for field, value in data.items():
                                    if value and str(value).strip():
                                        enhanced_query += f"\n       {field}: {value}"

                        # Add related genes separately for clarity
                        if "related_genes" in relevant_data and relevant_data["related_genes"]:
                            enhanced_query += "\n     Related Genes:"
                            for gene in relevant_data["related_genes"][
                                :3
                            ]:  # Limit to first 3 genes
                                enhanced_query += (
                                    f"\n       - {gene.get('Gene_Name', 'Unknown Gene')}"
                                )
                                if "Protein_Name" in gene and gene["Protein_Name"]:
                                    enhanced_query += f" ({gene['Protein_Name']})"
                                if "Molecule_Role" in gene and gene["Molecule_Role"]:
                                    enhanced_query += f"\n         Role: {gene['Molecule_Role']}"

            # Add which match to use
            if sorted_matches:
                enhanced_query += f"\n  Use '{sorted_matches[0]['synonym']}' as the primary match for '{query_entity}'"

    # Add specific instructions for the agent
    full_query = f"""
    Question: {enhanced_query}

    Please provide a comprehensive answer using information from the VIOLIN database tables.
    For your reference, here are the available tables:
    - Vaccine_Information.csv: Contains vaccine details
    - Pathogen_Information.csv: Contains pathogen information
    - Gene_Information.csv: Contains gene information
    - Gene_Vaccine_Pathogen_Information.csv: Maps genes, vaccines, and pathogens
    - Vaccine_Pathogen_Information.csv: Maps vaccines and pathogens

    In your answer:
    1. Specify which tables you used to find the information
    2. Clearly label all fields from the database tables
    3. If combining information from multiple tables, explain the relationships
    4. If statistical analysis is required, provide summaries
    5. Include the relevant information for each entity such as pathogen details (VIOLIN_c_pathogen_id,
       Host_Protective_Immunity, Host_Ranges_and_Animal_Models) and gene descriptions if available in the database
    """

    # Run the VIOLIN agent on the enhanced query
    result = violin_agent.invoke({"input": full_query})

    # Extract the response from the agent
    violin_response = result.get("output", "")

    # Post-process the VIOLIN response
    processed_violin_response = post_process_response(violin_response, entity_data)

    # Process BV-BRC query if genome entities or pathogen entities were detected
    bvbrc_response = ""
    if genome_entities or pathogen_entities:
        bvbrc_response = process_bvbrc_query(
            user_query, genome_entities, pathogen_entities=pathogen_entities, verbose=verbose
        )

    # Combine the responses
    combined_response = processed_violin_response
    if bvbrc_response:
        combined_response += bvbrc_response

    return combined_response


def process_bvbrc_query(
    user_query: str,
    genome_entities: list[dict[str, Any]],
    pathogen_entities: list[dict[str, Any]] = None,
    verbose: bool = False,
) -> str:
    """
    Process a query specifically for BV-BRC genome data.

    Args:
        user_query: The original user query
        genome_entities: List of detected genome entities from the query
        pathogen_entities: List of detected pathogen entities from the query
        verbose: Whether to enable verbose mode

    Returns:
        Response from the BV-BRC agent
    """
    # If no genome or pathogen entities, return empty response
    if not genome_entities and not pathogen_entities:
        return ""

    # Initialize the BV-BRC agent
    bvbrc_agent = initialize_bvbrc_agent(verbose=verbose)

    # Store query type for customizing the response later
    query_types = []
    if genome_entities:
        query_types.append("genome")
    if pathogen_entities:
        query_types.append("pathogen")

    # Find matched genome terms using consolidated_synonym_search
    genome_matches = []

    # Process direct genome entities
    if genome_entities:
        for entity in genome_entities:
            entity_query = entity["name"]
            matches = consolidated_synonym_search(
                entity_query,
                dfs={"bvbrc_genomes": _get_dfs().get("bvbrc_genomes", pd.DataFrame())},
                include_relevant_data=True,
            )
            genome_matches.extend(matches)

    # Process pathogen entities and find related genomes
    pathogen_matches = []
    if pathogen_entities:
        for entity in pathogen_entities:
            entity_query = entity["name"]

            # Try to find pathogen information in the bvbrc_genomes dataframe
            current_pathogen_matches = []

            # Create a dedicated search specifically for pathogens in the genome database
            _bvbrc_dfs = _get_dfs()
            if "bvbrc_genomes" in _bvbrc_dfs:
                bvbrc_df = _bvbrc_dfs["bvbrc_genomes"]
                # Search in pathogen-related columns like Family, Genus, Species
                pathogen_columns = ["Family", "Genus", "Species", "Strain", "Taxon Lineage Names"]
                mask = pd.Series(False, index=bvbrc_df.index)

                for col in pathogen_columns:
                    if col in bvbrc_df.columns:
                        col_mask = (
                            bvbrc_df[col]
                            .fillna("")
                            .astype(str)
                            .str.contains(entity_query, case=False, regex=False)
                        )
                        mask = mask | col_mask

                matched_rows = bvbrc_df[mask]

                if not matched_rows.empty:
                    # Create matches for the first 5 results
                    for idx, row in matched_rows.head(5).iterrows():
                        # Use the most specific taxonomy level available as the match name
                        if pd.notna(row.get("Species")):
                            match_name = row["Species"]
                        elif pd.notna(row.get("Genus")):
                            match_name = row["Genus"]
                        elif pd.notna(row.get("Family")):
                            match_name = row["Family"]
                        else:
                            match_name = row.get("Genome Name", f"Genome {idx}")

                        # Create a match object
                        match = {
                            "query_entity": entity_query,
                            "synonym": match_name,
                            "score": 0.8,  # Arbitrary score for pathogen-derived matches
                            "relevant_data": {
                                "bvbrc_genomes": {
                                    col: row[col]
                                    for col in row.index
                                    if col
                                    in [
                                        "Genome ID",
                                        "Genome Name",
                                        "Family",
                                        "Genus",
                                        "Species",
                                        "Strain",
                                        "Host Name",
                                        "Geographic Location",
                                    ]
                                    and pd.notna(row[col])
                                }
                            },
                        }
                        current_pathogen_matches.append(match)

            # If no specific matches found, also try to get matches using the generic synonym search
            if not current_pathogen_matches:
                direct_matches = consolidated_synonym_search(
                    entity_query,
                    dfs={"bvbrc_genomes": _get_dfs().get("bvbrc_genomes", pd.DataFrame())},
                    include_relevant_data=True,
                )
                current_pathogen_matches.extend(direct_matches)

            pathogen_matches.extend(current_pathogen_matches)

    # Combine all matches
    all_matches = genome_matches + pathogen_matches

    # If no matches found, return generic genome query based on the entity types
    if not all_matches:
        if "genome" in query_types and "pathogen" in query_types:
            # Both genome and pathogen entities present but no matches
            bvbrc_query = f"""
            Question: {user_query}

            You were asked about genome data related to specific pathogens, but no exact matches were found.

            Entities mentioned:
            - Genome entities: {", ".join(entity["name"] for entity in genome_entities)}
            - Pathogen entities: {", ".join(entity["name"] for entity in pathogen_entities)}

            Please provide:
            1. General information about alphavirus genomes that might be related to these entities
            2. Suggest similar or related genomes in the database
            3. Include taxonomic relationships if relevant
            4. Suggest specific genomic characteristics to look for in these types of viruses
            """
        elif "genome" in query_types:
            # Only genome entities present but no matches
            bvbrc_query = f"""
            Question: {user_query}

            You were asked about specific genome data, but no exact matches were found for:
            {", ".join(entity["name"] for entity in genome_entities)}

            Please provide:
            1. Information about similar alphavirus genomes in the database
            2. General characteristics of alphavirus genomes
            3. Common genomic features and structures
            """
        else:
            # Only pathogen entities present but no matches
            bvbrc_query = f"""
            Question: {user_query}

            You were asked about pathogens, but no exact genomic matches were found for:
            {", ".join(entity["name"] for entity in pathogen_entities)}

            Please provide:
            1. Information about alphavirus genomes taxonomically related to these pathogens
            2. Overview of genome characteristics for the Alphavirus genus or related taxonomic groups
            3. Compare and contrast different alphavirus genomes if relevant
            """
    else:
        # Group matches by entity
        entity_matches = {}
        for match in all_matches:
            query_entity = match["query_entity"]
            if query_entity not in entity_matches:
                entity_matches[query_entity] = []
            entity_matches[query_entity].append(match)

        # Build an enhanced query with the matches
        bvbrc_query = f"""
        Question: {user_query}

        I've identified the following entities in the BV-BRC genome database:
        """

        for query_entity, matches in entity_matches.items():
            # Sort matches by score
            sorted_matches = sorted(matches, key=lambda x: x.get("score", 0), reverse=True)

            # Add entity and its matches
            entity_type = next(
                (
                    e["type"]
                    for e in genome_entities + (pathogen_entities or [])
                    if e["name"] == query_entity
                ),
                "unknown",
            )

            bvbrc_query += f"\n\n- Entity: '{query_entity}' (Type: {entity_type})"
            bvbrc_query += "\n  Matches in database:"

            # Add top matches
            for i, match in enumerate(sorted_matches[:3], 1):
                synonym = match["synonym"]
                score = match["score"]
                bvbrc_query += f"\n  {i}. '{synonym}' (Score: {score:.2f})"

                # Add relevant data
                if "relevant_data" in match and "bvbrc_genomes" in match["relevant_data"]:
                    data = match["relevant_data"]["bvbrc_genomes"]
                    bvbrc_query += "\n     Key information:"
                    for key, value in data.items():
                        if value and str(value).strip():
                            bvbrc_query += f"\n     - {key}: {value}"

        # Add instructions based on entity types
        if "genome" in query_types and "pathogen" in query_types:
            bvbrc_query += """

            Please provide:
            1. Detailed genomic information for each matched genome
            2. Compare genomes associated with each pathogen entity
            3. Highlight taxonomic relationships between the matched genomes
            4. Include size, GC content, and other genomic features
            5. Discuss host range and geographic distribution if relevant
            """
        elif "genome" in query_types:
            bvbrc_query += """

            Please provide:
            1. Detailed information about each matched genome
            2. Genomic structure, size, and composition
            3. Key genetic elements and features
            4. Host and geographic information if available
            """
        else:
            bvbrc_query += """

            Please provide:
            1. Overview of genomic characteristics for each pathogen
            2. Compare genomes within the same taxonomic groups
            3. Highlight common and distinguishing genomic features
            4. Include information about genome size, structure, and composition
            5. Discuss distribution patterns across hosts and geographic regions
            """

    # Run the BV-BRC agent on the enhanced query
    result = bvbrc_agent.invoke({"input": bvbrc_query})

    # Extract and format the response
    response = result.get("output", "")

    # Add a header to indicate this is BV-BRC data
    if response:
        if "genome" in query_types and "pathogen" in query_types:
            response = "\n\n## BV-BRC Genome Information (Genome and Pathogen Data)\n\n" + response
        elif "genome" in query_types:
            response = "\n\n## BV-BRC Genome Information\n\n" + response
        else:
            response = "\n\n## BV-BRC Pathogen Genome Information\n\n" + response

    return response


def main() -> None:
    """Main function to run the BV-BRC and VIOLIN database natural language interface."""
    print("\n" + "=" * 80)
    print("BV-BRC and VIOLIN Database Natural Language Interface")
    print("=" * 80)

    # Display database statistics
    stats = get_agent_statistics()
    print("\nDatabase Statistics:")
    for key, value in stats.items():
        print(f"  {key}: {value}")

    # Interactive query loop
    print("\nEnter your questions about vaccines, pathogens, or genes (or 'exit' to quit):")

    while True:
        query = input("\nQuery: ")
        if query.lower() in ["exit", "quit", "q"]:
            break

        print("\nProcessing...\n")
        try:
            result = process_query(query)
            print(f"Result:\n{result}")
        except Exception as e:
            print(f"An error occurred: {str(e)}")

    print("\nThank you for using the BV-BRC and VIOLIN Database Natural Language Interface!")


__all__ = [
    "SYSTEM_PROMPT",
    "initialize_csv_agent",
    "initialize_bvbrc_agent",
    "post_process_response",
    "process_query",
    "process_bvbrc_query",
    "main",
]


if __name__ == "__main__":
    main()
