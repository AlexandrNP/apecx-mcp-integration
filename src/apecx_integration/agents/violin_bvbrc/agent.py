"""
BV-BRC and VIOLIN Database Natural Language Interface

This script creates a natural language interface for the BV-BRC and VIOLIN databases
using Langchain CSV agents to query and retrieve information from multiple
CSV tables containing vaccine, pathogen, and gene information.

LLM backend (env-configurable; defaults target a local Ollama daemon):
    APECX_LLM_BASE_URL    OpenAI-compatible endpoint URL (default
                          ``http://localhost:11434/v1``).
    APECX_LLM_MODEL       Model name (default ``mistral-small:latest``).
    APECX_LLM_API_KEY     API key. Falls back to ``OPENAI_API_KEY`` if set,
                          then to ``"EMPTY"`` for local backends that do
                          not validate keys (Ollama, vLLM).
    APECX_LLM_TEMPERATURE Float; overrides per-call temperature when set.
                          Defaults to the per-call value (typically 0.0).
    APECX_LLM_MAX_TOKENS  Int; overrides per-call max_tokens when set.
                          Use to bound cost / wall-time for downstream
                          operators (e.g., 256 for fast dev loop, 1024 for
                          quality-sensitive paths).

Data files (operator-provided; see README):
    APECX_DB_DATA_DIR    Directory containing the VIOLIN + BV-BRC CSVs.
                         Defaults to the repo root (where the committed
                         ``BVBRC_genome_alphavirus.csv`` lives). The
                         VIOLIN tables must be dropped here by the
                         operator — they are NOT part of the package.
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage

# LangChain imports.
#
# Only the symbols used by the three public entry points
# (``extract_entities_llm``, ``consolidated_synonym_search``,
# ``enrich_matches_with_database_data``) stay at module scope. Imports
# that are only needed by the dormant CSV-agent paths
# (``initialize_csv_agent`` / ``initialize_bvbrc_agent``) are inlined
# at call time — those paths depend on ``langchain_experimental`` + the
# older ``langchain.agents.agent_types.AgentType`` shape, and langchain
# 1.x reorganised both. Failing at call time instead of import time
# keeps the packaging loadable for everyone who doesn't touch CSV agents.
from langchain_openai import ChatOpenAI

# Load environment variables from a .env file in the caller's CWD if
# present. Does NOT mutate process env for vars that are already set,
# so operator-provided APECX_LLM_* / OPENAI_API_KEY values win.
load_dotenv()

logger = logging.getLogger(__name__)

# Cluster AR (2026-04-27) — max candidates per category fed to the
# LLM in ``consolidated_synonym_search``. Was an unnamed ``[:100]``
# magic number that produced silent alphabetical-first-100
# truncation against the real VIOLIN catalog (3500+ vaccines, 3600+
# genes). Fix uses ``filter_candidates_by_similarity`` to select
# the 100 most-relevant-to-the-query candidates per category, NOT
# the alphabetical first 100. See test_probe_batch_34_cluster_ar
# in apecx-mcp-integration for the regression mat.
MAX_CANDIDATES_PER_CATEGORY = 100

# Define CSV file paths (filenames relative to APECX_DB_DATA_DIR).
CSV_FILES = {
    "vaccines": "Vaccine_Information.csv",
    "pathogens": "Pathogen_Information.csv",
    "genes": "Gene_Information.csv",
    "gene_vaccine_pathogen": "Gene_Vaccine_Pathogen_Information.csv",
    "vaccine_pathogen": "Vaccine_Pathogen_Information.csv",
    "bvbrc_genomes": "BVBRC_genome_alphavirus.csv"
}


def _default_data_dir() -> Path:
    """Default data directory: the apecx-db-integration repo root,
    where the committed ``BVBRC_genome_alphavirus.csv`` + the five
    untracked VIOLIN CSVs live on developer machines.

    Traversal: ``src/apecx_db_integration/agent.py`` → ``repo_root``.
    Resolving via ``__file__`` means the path is stable regardless of
    the caller's CWD, which used to be the foot-gun before packaging.
    """
    return Path(__file__).resolve().parents[2]


def load_dataframes() -> dict[str, pd.DataFrame]:
    """Read the CSV tables from ``APECX_DB_DATA_DIR`` (or the default).

    Missing files are logged and skipped rather than raised — downstream
    code already guards for missing keys in the returned dict. Keeping
    that legacy contract; a future pass may promote a hard failure when
    a required table is absent.
    """
    data_dir_env = os.environ.get("APECX_DB_DATA_DIR")
    data_dir = Path(data_dir_env) if data_dir_env else _default_data_dir()
    dataframes: dict[str, pd.DataFrame] = {}
    for key, filename in CSV_FILES.items():
        file_path = data_dir / filename
        if file_path.is_file():
            dataframes[key] = pd.read_csv(file_path)
        else:
            logger.warning("CSV table not found: %s (key=%s)", file_path, key)
    return dataframes


# Lazy DFS — the previous module-top ``DFS = load_dataframes()`` read
# six CSV files from the caller's CWD at *import time*. That made the
# whole module unimportable without the data present. Now we lazy-init
# on first attribute access and keep the public ``DFS`` name via
# PEP-562 ``__getattr__``.
_DFS_CACHE: dict[str, pd.DataFrame] | None = None


def _get_dfs() -> dict[str, pd.DataFrame]:
    """Internal: cached access to the VIOLIN + BV-BRC dataframes."""
    global _DFS_CACHE
    if _DFS_CACHE is None:
        _DFS_CACHE = load_dataframes()
    return _DFS_CACHE


def __getattr__(name: str):
    """PEP-562 module-level ``__getattr__`` so external callers that do
    ``from apecx_db_integration import agent; agent.DFS`` keep working
    against the lazy cache.

    NOTE: this hook fires only on attribute access on the module object
    (``module.DFS``). Bare-name references inside this module compile
    to ``LOAD_GLOBAL`` and do NOT route through here — call ``_get_dfs()``
    directly instead.
    """
    if name == "DFS":
        return _get_dfs()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _build_chat_llm(
    temperature: float = 0.0,
    max_tokens: int = 1024,
    **overrides: Any,
) -> ChatOpenAI:
    """Build a LangChain ``ChatOpenAI`` instance against the configured
    endpoint. Defaults to the local Ollama daemon; override via env vars
    or per-call kwargs.

    Resolution order for ``temperature`` and ``max_tokens``:

      env var > caller kwarg (explicit) > function default

    Env vars (``APECX_LLM_TEMPERATURE`` / ``APECX_LLM_MAX_TOKENS``) win
    so that operators can tune cost/quality bounds without re-deploying
    or editing wrapper YAMLs. Caller kwargs win over the function
    defaults for callers that need a specific shape (e.g., a CSV agent
    that genuinely needs ``max_tokens=16384`` regardless of operator
    policy).

    ``ChatOpenAI`` speaks the OpenAI chat-completions protocol, which
    Ollama and vLLM both implement at their ``/v1`` endpoints — no
    separate ``ChatOllama`` / ``ChatvLLM`` wrapper needed.
    """
    base_url = os.environ.get("APECX_LLM_BASE_URL", "http://localhost:11434/v1")
    model = os.environ.get("APECX_LLM_MODEL", "mistral-small:latest")
    api_key = (
        os.environ.get("APECX_LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or "EMPTY"
    )
    env_temperature = os.environ.get("APECX_LLM_TEMPERATURE")
    if env_temperature is not None:
        temperature = float(env_temperature)
    env_max_tokens = os.environ.get("APECX_LLM_MAX_TOKENS")
    if env_max_tokens is not None:
        max_tokens = int(env_max_tokens)
    kwargs: dict[str, Any] = {
        "temperature": temperature,
        "model": model,
        "max_tokens": max_tokens,
        "base_url": base_url,
        "api_key": api_key,
    }
    kwargs.update(overrides)
    return ChatOpenAI(**kwargs)


def get_agent_statistics() -> dict[str, int]:
    """Get basic statistics about the database tables."""
    stats = {}
    for key, df in _get_dfs().items():
        stats[f"{key}_count"] = len(df)
    return stats


# Create an LLM instance for entity extraction and synonym searching
def get_llm_for_entity_extraction():
    """Get an LLM instance optimized for entity extraction."""
    return _build_chat_llm(temperature=0, max_tokens=1024)


# Entity and synonym functionality using LLM
def extract_entities_llm(query: str) -> list[dict[str, Any]]:
    """
    Extract potential entities from a user query using an LLM.
    
    Args:
        query: The user query
        
    Returns:
        List of dictionaries containing entity information with keys:
        - name: The entity name
        - type: The entity type (e.g., 'pathogen', 'vaccine', 'gene')
        - confidence: Confidence score (0-1)
    """
    llm = get_llm_for_entity_extraction()

    system_prompt = """
    You are an expert biomedical entity extractor. Your task is to identify entities 
    in a user query related to vaccines, pathogens, genes, and genomes. 
    
    Entities can be:
    - Pathogens (viruses, bacteria, fungi, parasites)
    - Vaccines (by name, type, or target)
    - Genes/Proteins
    - Genomes (genomic data, sequences, or specific viral genomes)
    - Diseases
    - Medical terms relevant to immunology or virology
    
    For each entity you identify, provide:
    1. The entity name exactly as it appears in the text
    2. The entity type (pathogen, vaccine, gene, genome, disease, or medical_term)
    3. A confidence score (0-1) of how certain you are this is a relevant entity
    
    Format your response as a JSON array of objects with keys: name, type, confidence
    """

    human_prompt = f"""
    Please extract all biomedical entities from this query: "{query}"
    
    Return only the JSON with no explanations or additional text.
    """

    response = llm.invoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=human_prompt)
    ])

    # Parse the JSON response
    try:
        entities = json.loads(response.content)
        # Filter out low confidence entities
        entities = [entity for entity in entities if entity.get('confidence', 0) >= 0.5]
        return entities
    except json.JSONDecodeError:
        # If JSON parsing fails, try to extract with regex as fallback
        content = response.content
        entities_match = re.search(r'\[.*\]', content, re.DOTALL)
        if entities_match:
            try:
                entities = json.loads(entities_match.group(0))
                return entities
            except Exception:
                return []
        return []

def get_candidate_terms(dfs: dict[str, pd.DataFrame] = None) -> dict[str, list[str]]:
    """
    Extract all potential candidate terms from the database that could be matches for entities.
    
    Returns:
        Dictionary mapping entity types to lists of candidate terms
    """
    if dfs is None:
        dfs = _get_dfs()

    candidates = {
        "pathogen": [],
        "vaccine": [],
        "gene": [],
        "disease": [],
        "medical_term": [],
        "genome": []  # Added genome type for BVBRC data
    }

    # Extract searchable columns from each table
    searchable_columns = {
        "vaccines": {
            "vaccine": ["Vaccine", "Vaccine_Name", "Tradename", "Product_Name"],
            "medical_term": ["Type", "Antigen", "Description", "Preparation"]
        },
        "pathogens": {
            "pathogen": ["Pathogen", "Family", "Genus", "Species"],
            "disease": ["Disease"],
            "medical_term": ["Pathogen_Description", "Microbial_Pathogenesis"]
        },
        "genes": {
            "gene": ["Gene_Name", "Protein_Name"],
            "medical_term": ["Molecule_Role", "Function"]
        },
        "bvbrc_genomes": {
            "genome": ["Genome ID", "Genome Name", "Other Names"],
            "pathogen": ["Genus", "Species", "Strain", "Family"],
            "disease": [],
            "medical_term": ["Host Name", "Host Common Name", "Geographic Location"]
        }
    }

    # Collect all unique terms from each category
    for df_name, column_types in searchable_columns.items():
        if df_name not in dfs:
            continue

        df = dfs[df_name]

        for entity_type, columns in column_types.items():
            for col in columns:
                if col in df.columns:
                    # Extract unique values, exclude nulls and very short terms
                    values = df[col].dropna().astype(str)
                    values = values[values.str.len() > 2]
                    candidates[entity_type].extend(values.unique().tolist())

    # Remove duplicates
    for entity_type in candidates:
        candidates[entity_type] = list(set(candidates[entity_type]))

    return candidates

def rank_terms_with_llm(entity: dict[str, Any], candidates: dict[str, list[str]]) -> list[dict[str, Any]]:
    """
    Use LLM to rank candidate terms based on how likely they refer to the same entity.
    
    Args:
        entity: Dictionary containing entity information (name, type, confidence)
        candidates: Dictionary mapping entity types to lists of candidate terms
        
    Returns:
        List of dictionaries with candidate terms and their similarity scores
    """
    llm = get_llm_for_entity_extraction()

    entity_name = entity['name']
    entity_type = entity['type']

    # Determine which candidate list to use
    # First try exact type match, then fallback to other types if needed
    potential_candidates = []
    if entity_type in candidates and candidates[entity_type]:
        potential_candidates = candidates[entity_type]
    else:
        # Fallback to all candidate types
        for _candidate_type, candidate_list in candidates.items():
            potential_candidates.extend(candidate_list)

    # If we have too many candidates, select a subset for efficiency
    if len(potential_candidates) > 100:
        # Do a preliminary filter using string similarity
        potential_candidates = filter_candidates_by_similarity(entity_name, potential_candidates, max_candidates=100)

    # Ensure we have a reasonable number for the LLM to process
    if len(potential_candidates) > 20:
        potential_candidates = potential_candidates[:20]

    if not potential_candidates:
        return []

    system_prompt = """
    You are an expert in biomedical terminology and entity matching. Your task is to determine
    how likely each candidate term refers to the same entity as the query term.
    
    Assess each candidate and provide:
    1. A similarity score (0-1) indicating how likely the candidate refers to the same entity
    2. A brief reason for your assessment (1-2 words)
    
    Consider:
    - Exact matches and synonyms should have high scores
    - Related terms in the same taxonomic family or class should have moderate scores
    - Terms that refer to completely different entities should have low scores
    
    Format your response as a JSON array of objects with keys: term, score, reason
    """

    human_prompt = f"""
    Query term: "{entity_name}" (Type: {entity_type})
    
    Candidate terms to evaluate:
    {json.dumps(potential_candidates, indent=2)}
    
    Return only the JSON with no explanations or additional text.
    """

    response = llm.invoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=human_prompt)
    ])

    # Parse the JSON response
    try:
        ranked_terms = json.loads(response.content)
        # Sort by score in descending order
        ranked_terms = sorted(ranked_terms, key=lambda x: x.get('score', 0), reverse=True)
        return ranked_terms
    except json.JSONDecodeError:
        # If JSON parsing fails, try to extract with regex as fallback
        content = response.content
        terms_match = re.search(r'\[.*\]', content, re.DOTALL)
        if terms_match:
            try:
                ranked_terms = json.loads(terms_match.group(0))
                ranked_terms = sorted(ranked_terms, key=lambda x: x.get('score', 0), reverse=True)
                return ranked_terms
            except Exception:
                return []
        return []

def filter_candidates_by_similarity(query: str, candidates: list[str], max_candidates: int = 100) -> list[str]:
    """
    Filter candidate terms using simple string similarity to reduce the number of candidates.
    
    Args:
        query: The query string
        candidates: List of candidate strings
        max_candidates: Maximum number of candidates to return
        
    Returns:
        Filtered list of candidates
    """
    # Simple case-insensitive contains check
    query_lower = query.lower()
    direct_matches = [c for c in candidates if query_lower in c.lower() or c.lower() in query_lower]

    # If we have enough direct matches, return them
    if len(direct_matches) >= max_candidates:
        return direct_matches[:max_candidates]

    # Otherwise, add candidates with word overlap
    query_words = set(query_lower.split())
    remaining_slots = max_candidates - len(direct_matches)

    word_overlap_scores = []
    for candidate in candidates:
        if candidate in direct_matches:
            continue

        candidate_words = set(candidate.lower().split())
        overlap = len(query_words.intersection(candidate_words))
        if overlap > 0:
            word_overlap_scores.append((candidate, overlap))

    # Sort by overlap score (descending)
    word_overlap_scores.sort(key=lambda x: x[1], reverse=True)
    overlap_matches = [candidate for candidate, score in word_overlap_scores[:remaining_slots]]

    return direct_matches + overlap_matches

def llm_find_synonyms(entity_dict: dict[str, Any], dfs: dict[str, pd.DataFrame] = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Find synonyms for an entity using LLM ranking.
    
    Args:
        entity_dict: Entity dictionary with name, type, and confidence
        dfs: Dictionary of dataframes to search in
        
    Returns:
        Tuple containing:
            - List of dictionaries with ranked terms and their scores
            - Metadata about the process
    """
    if dfs is None:
        dfs = _get_dfs()

    # Get all candidate terms from the database
    all_candidates = get_candidate_terms(dfs)

    # Rank the terms using LLM
    ranked_terms = rank_terms_with_llm(entity_dict, all_candidates)

    # Metadata about the synonym search process
    metadata = {
        "entity": entity_dict,
        "candidate_counts": {k: len(v) for k, v in all_candidates.items()},
        "ranked_count": len(ranked_terms)
    }

    return ranked_terms, metadata

def enrich_query_with_llm_synonyms(query: str) -> str:
    """
    Enhance a query by adding synonyms for detected entities using LLM-based ranking.
    
    Args:
        query: The original user query
        
    Returns:
        An enhanced query with synonym information
    """
    # Extract entities using LLM
    entities = extract_entities_llm(query)

    if not entities:
        return query

    # Find synonyms for each entity
    entity_results = {}
    for entity in entities:
        ranked_terms, metadata = llm_find_synonyms(entity)
        if ranked_terms:
            entity_results[entity['name']] = {
                'ranked_terms': ranked_terms,
                'metadata': metadata
            }

    # If no synonyms found, return original query
    if not entity_results:
        return query

    # Construct enhanced query
    enhanced_query = query + "\n\nI detected these entities and their potential matches in our database:"

    for entity_name, result in entity_results.items():
        ranked_terms = result['ranked_terms']
        entity_type = result['metadata']['entity']['type']

        # Get top 3 results
        top_results = ranked_terms[:3]
        if top_results:
            # Add entity information
            enhanced_query += f"\n\n- Entity: '{entity_name}' (Type: {entity_type})"
            enhanced_query += "\n  Most likely matches in database:"

            # Add top matches with scores
            for i, term_info in enumerate(top_results, 1):
                term = term_info.get('term', '')
                score = term_info.get('score', 0)
                reason = term_info.get('reason', '')
                enhanced_query += f"\n  {i}. '{term}' (Similarity: {score:.2f}, Reason: {reason})"

            # Add how to interpret and use these matches
            enhanced_query += f"\n  Use '{top_results[0]['term']}' as the primary match for '{entity_name}'"

    return enhanced_query

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
        CSV_FILES["vaccine_pathogen"]
    ]

    agent = create_csv_agent(
        llm,
        violin_csv_files,
        agent_type=AgentType.OPENAI_FUNCTIONS,
        verbose=verbose,
        allow_dangerous_code=True,
        max_iterations=1000,
        max_execution_time=600,
        extra_tools=[PythonREPLTool()]
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
        system_message=system_message
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
    if entity_data and 'matches' in entity_data and entity_data['matches']:
        response += "\n\n---\n\nDetailed Database Information:\n"

        # Group matches by entity type for organized display
        grouped_matches = {}
        for match in entity_data['matches']:
            if 'relevant_data' in match:
                query_entity = match['query_entity']
                entity_type = next((entity['type'] for entity in extract_entities_llm(query_entity)
                                  if entity['confidence'] >= 0.7), "unknown")

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
                entity_name = match['query_entity']
                response += f"\n### {entity_name} ({match['synonym']})\n"

                # Format and add the relevant data
                if 'relevant_data' in match:
                    for table, data in match['relevant_data'].items():
                        if table == "pathogens":
                            response += "**VIOLIN Pathogen Data:**\n"
                            priority_fields = ["VIOLIN_c_pathogen_id", "Pathogen", "NCBI_Taxonomy_ID", "Disease"]
                            response = add_data_fields(response, data, priority_fields)

                        elif table == "vaccines":
                            response += "**VIOLIN Vaccine Data:**\n"
                            priority_fields = ["Vaccine", "Vaccine_Name", "Type", "Status", "Antigen"]
                            response = add_data_fields(response, data, priority_fields)

                        elif table == "genes":
                            response += "**VIOLIN Gene Data:**\n"
                            priority_fields = ["Gene_Name", "Organism", "VIOLIN_c_gene_id", "Protein_Name", "Molecule_Role"]
                            response = add_data_fields(response, data, priority_fields)

                        elif table == "bvbrc_genomes":
                            response += "**BV-BRC Genome Data:**\n"
                            priority_fields = ["Genome ID", "Genome Name", "Genus", "Species", "Strain",
                                             "GenBank Accessions", "Size", "GC Content", "Host Name",
                                             "Geographic Location", "Collection Year"]
                            response = add_data_fields(response, data, priority_fields)

                        elif table == "related_genes":
                            response += "**Related Genes:**\n"
                            for gene_data in data:
                                response += f"- {gene_data.get('Gene_Name', 'Unknown Gene')}"
                                if 'Protein_Name' in gene_data:
                                    response += f" ({gene_data['Protein_Name']})"
                                if 'Molecule_Role' in gene_data:
                                    response += f": {gene_data['Molecule_Role']}"
                                response += "\n"

    # Annotate field names in the response to improve readability
    field_pattern = r'\b(Name|Type|Status|Description|Efficacy|Disease|Pathogen|Species|Genus|Family|Date|Country|Function|Role|Host|Immunity|Pathogenesis|Genome)\b'
    response = re.sub(field_pattern, r'"\1"', response)

    return response

def process_query(user_query: str, verbose: bool = False) -> str:
    """Process a natural language query against the VIOLIN and BV-BRC databases."""
    # Extract entities to determine if we need to query BV-BRC database
    entities = extract_entities_llm(user_query)

    # Separate entities by type
    genome_entities = [entity for entity in entities if entity['type'] == 'genome']
    pathogen_entities = [entity for entity in entities if entity['type'] == 'pathogen']

    # Process VIOLIN database query (always run this)
    violin_agent = initialize_csv_agent(verbose=verbose)

    # Use the consolidated synonym search to find all entity matches with relevant data
    # Only search in VIOLIN tables for non-genome entities
    violin_dfs = {k: df for k, df in _get_dfs().items() if k != "bvbrc_genomes"}
    synonym_matches = consolidated_synonym_search(user_query, dfs=violin_dfs, include_relevant_data=True)

    # Build an enhanced query with the consolidated matches and relevant data
    enhanced_query = user_query

    # Store entity data for including in final response
    entity_data = {'matches': []}

    if synonym_matches:
        enhanced_query += "\n\nDetected entities and their matches in the VIOLIN database:"

        # Group results by query entity
        entity_matches = {}
        for match in synonym_matches:
            query_entity = match['query_entity']
            if query_entity not in entity_matches:
                entity_matches[query_entity] = []
            entity_matches[query_entity].append(match)

        # Add each entity's matches to the enhanced query
        for query_entity, matches in entity_matches.items():
            # Sort matches by score in descending order
            sorted_matches = sorted(matches, key=lambda x: x.get('score', 0), reverse=True)

            enhanced_query += f"\n\n- Entity: '{query_entity}'"
            enhanced_query += "\n  Top matches in database:"

            # Add the top 3 matches with relevant data if available
            for i, match in enumerate(sorted_matches[:3], 1):
                synonym = match['synonym']
                score = match['score']
                enhanced_query += f"\n  {i}. '{synonym}' (Similarity: {score:.2f})"

                # Add relevant data if available
                if 'relevant_data' in match:
                    relevant_data = match['relevant_data']
                    if relevant_data:
                        enhanced_query += "\n     Key information:"

                        # Also store for including in final response
                        entity_data['matches'].append(match)

                        # Add main entity information
                        for table_name, data in relevant_data.items():
                            if table_name != 'related_genes':
                                enhanced_query += f"\n     Table: {table_name}"
                                for field, value in data.items():
                                    if value and str(value).strip():
                                        enhanced_query += f"\n       {field}: {value}"

                        # Add related genes separately for clarity
                        if 'related_genes' in relevant_data and relevant_data['related_genes']:
                            enhanced_query += "\n     Related Genes:"
                            for gene in relevant_data['related_genes'][:3]:  # Limit to first 3 genes
                                enhanced_query += f"\n       - {gene.get('Gene_Name', 'Unknown Gene')}"
                                if 'Protein_Name' in gene and gene['Protein_Name']:
                                    enhanced_query += f" ({gene['Protein_Name']})"
                                if 'Molecule_Role' in gene and gene['Molecule_Role']:
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
        bvbrc_response = process_bvbrc_query(user_query, genome_entities, pathogen_entities=pathogen_entities, verbose=verbose)

    # Combine the responses
    combined_response = processed_violin_response
    if bvbrc_response:
        combined_response += bvbrc_response

    return combined_response


def consolidated_synonym_search(query: str, dfs: dict[str, pd.DataFrame] = None, include_relevant_data: bool = False) -> list[dict[str, Any]]:
    """
    Consolidate synonym search into a single LLM query that processes all entities at once.
    
    Args:
        query: The user query string
        dfs: Dictionary of dataframes to search in (optional)
        include_relevant_data: Whether to include relevant data from the tables for each matched entity
        
    Returns:
        List of dictionaries in format:
        [
            {
                'query_entity': Entity from user query,
                'synonym': Matching term from VIOLIN database,
                'score': Likelihood score that user meant this term,
                'relevant_data': Optional dictionary with related information from various tables
            }
        ]
    """
    if dfs is None:
        dfs = _get_dfs()

    # Step 1: Extract entities from the query
    entities = extract_entities_llm(query)
    if not entities:
        return []

    # Step 2: Get all candidate terms from the database
    all_candidates = get_candidate_terms(dfs)

    # Cluster AR (2026-04-27): pre-fix this function fed the LLM
    # ``{k: v[:100] for k, v in all_candidates.items()}`` — an
    # alphabetical-first-100 truncation of every category. Real
    # VIOLIN has 3,507 vaccines / 3,627 genes / 3,470 vaccine names;
    # 97% of the catalog was hidden from every synonym search call,
    # silently. There was no log/warn at the truncation site so the
    # failure mode was: tests pass against synthetic small dataframes,
    # production silently mis-matches against the alphabetical first
    # 100. See cluster AR notes in
    # ``apecx-mcp-integration/tests/integration/
    # test_probe_batch_34_cluster_ar_arch_gaps.py`` for the
    # regression mat.
    #
    # Fix: per-category similarity-filter against the query +
    # extracted-entity names so the candidates the LLM sees are
    # actually relevant to the query, not arbitrary alphabetical
    # neighbours. ``filter_candidates_by_similarity`` is the same
    # helper ``rank_terms_with_llm`` already uses for its
    # per-entity path; this brings the consolidated path up to
    # parity. Embedding-based selection is a Phase-2 follow-up;
    # this fix closes the hot bleed at minimal-diff cost.
    selection_query = " ".join(
        [query] + [e["name"] for e in entities]
    )
    filtered_candidates: dict[str, list[str]] = {}
    truncation_log: dict[str, tuple[int, int]] = {}
    for cat, cands in all_candidates.items():
        if len(cands) > MAX_CANDIDATES_PER_CATEGORY:
            filtered = filter_candidates_by_similarity(
                selection_query, cands,
                max_candidates=MAX_CANDIDATES_PER_CATEGORY,
            )
            filtered_candidates[cat] = filtered
            truncation_log[cat] = (len(cands), len(filtered))
        else:
            filtered_candidates[cat] = cands
    if truncation_log:
        # Surface the truncation as a single warning so an operator
        # reading the log sees the rate at which categories are
        # shrinking and can decide whether to expand the cap.
        logger.warning(
            "consolidated_synonym_search: applied similarity-based "
            "truncation to %d categor%s exceeding "
            "MAX_CANDIDATES_PER_CATEGORY=%d. Per-category "
            "(original_count -> kept_count): %s",
            len(truncation_log),
            "ies" if len(truncation_log) != 1 else "y",
            MAX_CANDIDATES_PER_CATEGORY,
            truncation_log,
        )

    # Step 3: Create a consolidated prompt for the LLM
    llm = get_llm_for_entity_extraction()

    system_prompt = """
    You are an expert in biomedical terminology and entity matching. Your task is to match entities 
    from a user query to their most likely matches in a biomedical database (VIOLIN).
    
    For each entity in the user query:
    1. Find the most likely matching terms in the database
    2. Assign a similarity score (0-1) to each potential match
    3. Return only the top matches
    
    Consider:
    - Exact matches and synonyms should have high scores (0.9-1.0)
    - Related terms in the same taxonomic family should have moderate scores (0.6-0.8)
    - Terms that refer to completely different entities should have low scores (<0.5)
    
    Format your response as a JSON array of objects with exactly these keys:
    - query_entity: The entity from the user query
    - synonym: The matching term from the VIOLIN database
    - score: The similarity score as a float between 0 and 1
    """

    # Create a human prompt with all the entities and candidates
    human_prompt = f"""
    User query: "{query}"
    
    Extracted entities:
    {json.dumps([{
        'name': entity['name'],
        'type': entity['type'],
        'confidence': entity['confidence']
    } for entity in entities], indent=2)}
    
    Unique terms in VIOLIN database by category:
    {json.dumps(filtered_candidates, indent=2)}
    
    For each extracted entity, find the top 3 most likely matching terms from the VIOLIN database.
    Return only the JSON array with no explanations or additional text.
    """

    response = llm.invoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=human_prompt)
    ])

    # Parse the JSON response
    try:
        # Attempt to parse the JSON directly
        matches = json.loads(response.content)

        # Validate the structure
        validated_matches = []
        for match in matches:
            if all(k in match for k in ['query_entity', 'synonym', 'score']):
                validated_matches.append(match)

        # If requested, enrich the matches with relevant data from the database tables
        if include_relevant_data and validated_matches:
            validated_matches = enrich_matches_with_database_data(validated_matches, dfs)

        return validated_matches
    except json.JSONDecodeError:
        # If direct parsing fails, try to extract JSON with regex
        content = response.content
        matches_match = re.search(r'\[.*\]', content, re.DOTALL)
        if matches_match:
            try:
                matches = json.loads(matches_match.group(0))
                validated_matches = []
                for match in matches:
                    if all(k in match for k in ['query_entity', 'synonym', 'score']):
                        validated_matches.append(match)

                # If requested, enrich the matches with relevant data from the database tables
                if include_relevant_data and validated_matches:
                    validated_matches = enrich_matches_with_database_data(validated_matches, dfs)

                return validated_matches
            except Exception:
                return []
        return []

def enrich_matches_with_database_data(matches: list[dict[str, Any]], dfs: dict[str, pd.DataFrame]) -> list[dict[str, Any]]:
    """
    Enriches the synonym matches with relevant data from the database tables.
    
    Args:
        matches: List of dictionaries with entity matches
        dfs: Dictionary of dataframes to search in
        
    Returns:
        Enriched list of dictionaries with added 'relevant_data' field
    """
    # Key fields to look for in each table by entity type
    important_fields = {
        "pathogen": {
            "pathogens": ["VIOLIN_c_pathogen_id", "Pathogen", "NCBI_Taxonomy_ID", "Disease",
                         "Pathogen_Description", "Microbial_Pathogenesis",
                         "Host_Ranges_and_Animal_Models", "Host_Protective_Immunity"],
            "bvbrc_genomes": ["Genome ID", "Genome Name", "NCBI Taxon ID", "Family", "Genus",
                             "Species", "Strain", "Isolation Country", "Geographic Location",
                             "Host Name", "Host Common Name"]
        },
        "vaccine": {
            "vaccines": ["Vaccine", "Vaccine_Name", "Vaccine_Ontology_ID", "Type", "Status",
                        "Host_Species_as_Laboratory_Animal_Model", "Immunization_Route", "Antigen",
                        "Manufacturer", "Location_Licensed", "Description", "Preparation", "Virulence"]
        },
        "gene": {
            "genes": ["Gene_Name", "Organism", "VIOLIN_c_gene_id", "NCBI_Gene_ID",
                     "Protein_Name", "Molecule_Role", "Locus_Tag", "Genbank_Accession"]
        },
        "genome": {
            "bvbrc_genomes": ["Genome ID", "Genome Name", "Other Names", "NCBI Taxon ID",
                             "Taxon Lineage Names", "Family", "Genus", "Species", "Strain",
                             "GenBank Accessions", "Size", "GC Content", "CDS",
                             "Isolation Country", "Geographic Location", "Host Name",
                             "Host Common Name", "Collection Date", "Collection Year"]
        }
    }

    # Function to find a term in a dataframe
    def find_term_in_df(term, df, columns_to_search):
        """Find rows where the term appears in any of the specified columns."""
        mask = pd.Series(False, index=df.index)
        for col in columns_to_search:
            if col in df.columns:
                # Handle NaN values and convert to string for comparison
                col_mask = df[col].fillna('').astype(str).str.contains(term, case=False, regex=False)
                mask = mask | col_mask
        return df[mask]

    # For each match, find relevant data in the database
    enriched_matches = []

    for match in matches:
        query_entity = match['query_entity']
        synonym = match['synonym']
        entity_type = next((e['type'] for e in extract_entities_llm(query_entity)), None)

        relevant_data = {}

        # Find relevant data in each applicable table based on entity type
        if entity_type in important_fields:
            for table_name, fields in important_fields[entity_type].items():
                if table_name in dfs:
                    # Search for the synonym in text columns
                    text_columns = [col for col in dfs[table_name].columns if dfs[table_name][col].dtype == 'object']
                    relevant_rows = find_term_in_df(synonym, dfs[table_name], text_columns)

                    if not relevant_rows.empty:
                        # Extract only important fields for this entity type
                        available_fields = [f for f in fields if f in relevant_rows.columns]
                        data = relevant_rows[available_fields].iloc[0].to_dict()
                        relevant_data[table_name] = data

        # For pathogens and vaccines, also look for related genes
        if entity_type in ['pathogen', 'vaccine'] and 'vaccine_pathogen' in dfs and 'gene_vaccine_pathogen' in dfs:
            # First, find vaccine-pathogen relationships
            vp_df = dfs['vaccine_pathogen']
            gene_vp_df = dfs['gene_vaccine_pathogen']
            genes_df = dfs.get('genes', pd.DataFrame())

            # Find pathogen or vaccine IDs
            pathogen_id = None
            vaccine_id = None

            # Try to extract IDs from relevant data
            if 'pathogens' in relevant_data and entity_type == 'pathogen':
                pathogen_id = relevant_data.get('pathogens', {}).get('VIOLIN_c_pathogen_id')
            elif 'vaccines' in relevant_data and entity_type == 'vaccine':
                vaccine_id = relevant_data.get('vaccines', {}).get('id')

            # Find vaccine-pathogen relationships
            vp_matches = pd.DataFrame()
            if pathogen_id is not None and 'VIOLIN_c_pathogen_id' in vp_df.columns:
                vp_matches = vp_df[vp_df['VIOLIN_c_pathogen_id'] == pathogen_id]
            elif vaccine_id is not None and 'vaccine_id' in vp_df.columns:
                vp_matches = vp_df[vp_df['vaccine_id'] == vaccine_id]

            # If we found any vaccine-pathogen relationships, look for associated genes
            if not vp_matches.empty and not gene_vp_df.empty and not genes_df.empty:
                vp_ids = vp_matches['id'].tolist() if 'id' in vp_matches.columns else []

                # Find genes associated with these vaccine-pathogen pairs
                gene_matches = gene_vp_df[gene_vp_df['vaccine_pathogen_id'].isin(vp_ids)] if 'vaccine_pathogen_id' in gene_vp_df.columns else pd.DataFrame()

                # Get gene details
                if not gene_matches.empty and 'gene_id' in gene_matches.columns:
                    gene_ids = gene_matches['gene_id'].tolist()
                    gene_details = genes_df[genes_df['id'].isin(gene_ids)] if 'id' in genes_df.columns else pd.DataFrame()

                    if not gene_details.empty:
                        # Select important gene fields
                        gene_fields = important_fields.get("gene", {}).get("genes", [])
                        available_gene_fields = [f for f in gene_fields if f in gene_details.columns]

                        # Add gene information
                        relevant_data['related_genes'] = gene_details[available_gene_fields].to_dict('records')

        # Add the relevant data to the match
        enriched_match = match.copy()
        if relevant_data:
            enriched_match['relevant_data'] = relevant_data

        enriched_matches.append(enriched_match)

    return enriched_matches

def process_bvbrc_query(user_query: str, genome_entities: list[dict[str, Any]], pathogen_entities: list[dict[str, Any]] = None, verbose: bool = False) -> str:
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
            entity_query = entity['name']
            matches = consolidated_synonym_search(entity_query,
                                                dfs={"bvbrc_genomes": _get_dfs().get("bvbrc_genomes", pd.DataFrame())},
                                                include_relevant_data=True)
            genome_matches.extend(matches)

    # Process pathogen entities and find related genomes
    pathogen_matches = []
    if pathogen_entities:
        for entity in pathogen_entities:
            entity_query = entity['name']

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
                        col_mask = bvbrc_df[col].fillna('').astype(str).str.contains(entity_query, case=False, regex=False)
                        mask = mask | col_mask

                matched_rows = bvbrc_df[mask]

                if not matched_rows.empty:
                    # Create matches for the first 5 results
                    for idx, row in matched_rows.head(5).iterrows():
                        # Use the most specific taxonomy level available as the match name
                        if pd.notna(row.get('Species')):
                            match_name = row['Species']
                        elif pd.notna(row.get('Genus')):
                            match_name = row['Genus']
                        elif pd.notna(row.get('Family')):
                            match_name = row['Family']
                        else:
                            match_name = row.get('Genome Name', f"Genome {idx}")

                        # Create a match object
                        match = {
                            "query_entity": entity_query,
                            "synonym": match_name,
                            "score": 0.8,  # Arbitrary score for pathogen-derived matches
                            "relevant_data": {
                                "bvbrc_genomes": {
                                    col: row[col] for col in row.index
                                    if col in ["Genome ID", "Genome Name", "Family", "Genus", "Species",
                                              "Strain", "Host Name", "Geographic Location"]
                                    and pd.notna(row[col])
                                }
                            }
                        }
                        current_pathogen_matches.append(match)

            # If no specific matches found, also try to get matches using the generic synonym search
            if not current_pathogen_matches:
                direct_matches = consolidated_synonym_search(entity_query,
                                                         dfs={"bvbrc_genomes": _get_dfs().get("bvbrc_genomes", pd.DataFrame())},
                                                         include_relevant_data=True)
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
            - Genome entities: {', '.join(entity['name'] for entity in genome_entities)}
            - Pathogen entities: {', '.join(entity['name'] for entity in pathogen_entities)}
            
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
            {', '.join(entity['name'] for entity in genome_entities)}
            
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
            {', '.join(entity['name'] for entity in pathogen_entities)}
            
            Please provide:
            1. Information about alphavirus genomes taxonomically related to these pathogens
            2. Overview of genome characteristics for the Alphavirus genus or related taxonomic groups
            3. Compare and contrast different alphavirus genomes if relevant
            """
    else:
        # Group matches by entity
        entity_matches = {}
        for match in all_matches:
            query_entity = match['query_entity']
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
            sorted_matches = sorted(matches, key=lambda x: x.get('score', 0), reverse=True)

            # Add entity and its matches
            entity_type = next((e['type'] for e in genome_entities + (pathogen_entities or [])
                              if e['name'] == query_entity), "unknown")

            bvbrc_query += f"\n\n- Entity: '{query_entity}' (Type: {entity_type})"
            bvbrc_query += "\n  Matches in database:"

            # Add top matches
            for i, match in enumerate(sorted_matches[:3], 1):
                synonym = match['synonym']
                score = match['score']
                bvbrc_query += f"\n  {i}. '{synonym}' (Score: {score:.2f})"

                # Add relevant data
                if 'relevant_data' in match and 'bvbrc_genomes' in match['relevant_data']:
                    data = match['relevant_data']['bvbrc_genomes']
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

if __name__ == "__main__":
    main()
