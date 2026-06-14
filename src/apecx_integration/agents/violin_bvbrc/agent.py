"""
BV-BRC and VIOLIN Database Natural Language Interface

This script creates a natural language interface for the BV-BRC and VIOLIN databases
using Langchain CSV agents to query and retrieve information from multiple
CSV tables containing vaccine, pathogen, and gene information.

LLM backend (env-configurable; defaults target a local Ollama daemon):
    APECX_LLM_BASE_URL    OpenAI-compatible endpoint URL (default
                          ``http://localhost:11434/v1``).
    APECX_LLM_MODEL       Model name (default ``nemotron-3-nano:4b``).
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
    "bvbrc_genomes": "BVBRC_genome_alphavirus.csv",
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


# LLM client factory. The canonical implementation lives in
# ``apecx_integration.agents._llm_factory``; this module re-binds it
# under the historical ``_build_chat_llm`` name so existing tests that
# do ``monkeypatch.setattr(violin_bvbrc.agent, "_build_chat_llm", ...)``
# keep working. Internal callers below reference the bare name
# ``_build_chat_llm`` so that monkeypatching this module attribute
# rebinds what they see (Python LEGB lookup at call time, not import
# time). New callers should import ``build_chat_llm`` directly from
# ``apecx_integration.agents._llm_factory``.
from apecx_integration.agents._llm_factory import build_chat_llm as _build_chat_llm  # noqa: E402


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

    response = llm.invoke(
        [SystemMessage(content=system_prompt), HumanMessage(content=human_prompt)]
    )

    # Parse the JSON response
    try:
        entities = json.loads(response.content)
        # Filter out low confidence entities
        entities = [entity for entity in entities if entity.get("confidence", 0) >= 0.5]
        return entities
    except json.JSONDecodeError:
        # If JSON parsing fails, try to extract with regex as fallback
        content = response.content
        entities_match = re.search(r"\[.*\]", content, re.DOTALL)
        if entities_match:
            try:
                entities = json.loads(entities_match.group(0))
                return entities
            except Exception as exc:
                # Degrade-loud: the LLM's entity-extraction output was unparseable even
                # after the regex fallback. [] is correct (no entities), but the operator
                # must know it was a PARSE FAILURE, not a genuine empty result.
                logger.warning(
                    "extract_entities_llm: LLM output unparseable after regex fallback "
                    "(%s: %s); returning no entities.",
                    type(exc).__name__,
                    exc,
                )
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
        "genome": [],  # Added genome type for BVBRC data
    }

    # Extract searchable columns from each table
    searchable_columns = {
        "vaccines": {
            "vaccine": ["Vaccine", "Vaccine_Name", "Tradename", "Product_Name"],
            "medical_term": ["Type", "Antigen", "Description", "Preparation"],
        },
        "pathogens": {
            "pathogen": ["Pathogen", "Family", "Genus", "Species"],
            "disease": ["Disease"],
            "medical_term": ["Pathogen_Description", "Microbial_Pathogenesis"],
        },
        "genes": {
            "gene": ["Gene_Name", "Protein_Name"],
            "medical_term": ["Molecule_Role", "Function"],
        },
        "bvbrc_genomes": {
            "genome": ["Genome ID", "Genome Name", "Other Names"],
            "pathogen": ["Genus", "Species", "Strain", "Family"],
            "disease": [],
            "medical_term": ["Host Name", "Host Common Name", "Geographic Location"],
        },
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


def rank_terms_with_llm(
    entity: dict[str, Any], candidates: dict[str, list[str]]
) -> list[dict[str, Any]]:
    """
    Use LLM to rank candidate terms based on how likely they refer to the same entity.

    Args:
        entity: Dictionary containing entity information (name, type, confidence)
        candidates: Dictionary mapping entity types to lists of candidate terms

    Returns:
        List of dictionaries with candidate terms and their similarity scores
    """
    llm = get_llm_for_entity_extraction()

    entity_name = entity["name"]
    entity_type = entity["type"]

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
        potential_candidates = filter_candidates_by_similarity(
            entity_name, potential_candidates, max_candidates=100
        )

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

    response = llm.invoke(
        [SystemMessage(content=system_prompt), HumanMessage(content=human_prompt)]
    )

    # Parse the JSON response
    try:
        ranked_terms = json.loads(response.content)
        # Sort by score in descending order
        ranked_terms = sorted(ranked_terms, key=lambda x: x.get("score", 0), reverse=True)
        return ranked_terms
    except json.JSONDecodeError:
        # If JSON parsing fails, try to extract with regex as fallback
        content = response.content
        terms_match = re.search(r"\[.*\]", content, re.DOTALL)
        if terms_match:
            try:
                ranked_terms = json.loads(terms_match.group(0))
                ranked_terms = sorted(ranked_terms, key=lambda x: x.get("score", 0), reverse=True)
                return ranked_terms
            except Exception as exc:
                # Degrade-loud: the LLM's term-ranking output was unparseable after the
                # regex fallback. [] degrades the ranking, but it must not be silent.
                logger.warning(
                    "rank_terms_with_llm: LLM output unparseable after regex fallback "
                    "(%s: %s); returning no ranked terms.",
                    type(exc).__name__,
                    exc,
                )
                return []
        return []


def filter_candidates_by_similarity(
    query: str, candidates: list[str], max_candidates: int = 100
) -> list[str]:
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


def llm_find_synonyms(
    entity_dict: dict[str, Any], dfs: dict[str, pd.DataFrame] = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
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
        "ranked_count": len(ranked_terms),
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
            entity_results[entity["name"]] = {"ranked_terms": ranked_terms, "metadata": metadata}

    # If no synonyms found, return original query
    if not entity_results:
        return query

    # Construct enhanced query
    enhanced_query = (
        query + "\n\nI detected these entities and their potential matches in our database:"
    )

    for entity_name, result in entity_results.items():
        ranked_terms = result["ranked_terms"]
        entity_type = result["metadata"]["entity"]["type"]

        # Get top 3 results
        top_results = ranked_terms[:3]
        if top_results:
            # Add entity information
            enhanced_query += f"\n\n- Entity: '{entity_name}' (Type: {entity_type})"
            enhanced_query += "\n  Most likely matches in database:"

            # Add top matches with scores
            for i, term_info in enumerate(top_results, 1):
                term = term_info.get("term", "")
                score = term_info.get("score", 0)
                reason = term_info.get("reason", "")
                enhanced_query += f"\n  {i}. '{term}' (Similarity: {score:.2f}, Reason: {reason})"

            # Add how to interpret and use these matches
            enhanced_query += (
                f"\n  Use '{top_results[0]['term']}' as the primary match for '{entity_name}'"
            )

    return enhanced_query


def consolidated_synonym_search(
    query: str, dfs: dict[str, pd.DataFrame] = None, include_relevant_data: bool = False
) -> list[dict[str, Any]]:
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
    selection_query = " ".join([query] + [e["name"] for e in entities])
    filtered_candidates: dict[str, list[str]] = {}
    truncation_log: dict[str, tuple[int, int]] = {}
    for cat, cands in all_candidates.items():
        if len(cands) > MAX_CANDIDATES_PER_CATEGORY:
            filtered = filter_candidates_by_similarity(
                selection_query,
                cands,
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
    {
        json.dumps(
            [
                {"name": entity["name"], "type": entity["type"], "confidence": entity["confidence"]}
                for entity in entities
            ],
            indent=2,
        )
    }

    Unique terms in VIOLIN database by category:
    {json.dumps(filtered_candidates, indent=2)}

    For each extracted entity, find the top 3 most likely matching terms from the VIOLIN database.
    Return only the JSON array with no explanations or additional text.
    """

    response = llm.invoke(
        [SystemMessage(content=system_prompt), HumanMessage(content=human_prompt)]
    )

    # Parse the JSON response
    try:
        # Attempt to parse the JSON directly
        matches = json.loads(response.content)

        # Validate the structure
        validated_matches = []
        for match in matches:
            if all(k in match for k in ["query_entity", "synonym", "score"]):
                validated_matches.append(match)

        # If requested, enrich the matches with relevant data from the database tables
        if include_relevant_data and validated_matches:
            validated_matches = enrich_matches_with_database_data(validated_matches, dfs)

        return validated_matches
    except json.JSONDecodeError:
        # If direct parsing fails, try to extract JSON with regex
        content = response.content
        matches_match = re.search(r"\[.*\]", content, re.DOTALL)
        if matches_match:
            try:
                matches = json.loads(matches_match.group(0))
                validated_matches = []
                for match in matches:
                    if all(k in match for k in ["query_entity", "synonym", "score"]):
                        validated_matches.append(match)

                # If requested, enrich the matches with relevant data from the database tables
                if include_relevant_data and validated_matches:
                    validated_matches = enrich_matches_with_database_data(validated_matches, dfs)

                return validated_matches
            except Exception as exc:
                # Degrade-loud: this block does MORE than JSON parsing — it validates the
                # matches and (when requested) enriches them via enrich_matches_with_database_data.
                # A bare `return []` here silently masked any real error in validation/enrichment
                # (e.g. a malformed dataframe) as "no matches". Log so a genuine failure is
                # visible and not mistaken for an empty synonym result.
                logger.warning(
                    "consolidated_synonym_search: match validation/enrichment failed "
                    "(%s: %s); returning no matches.",
                    type(exc).__name__,
                    exc,
                )
                return []
        return []


def enrich_matches_with_database_data(
    matches: list[dict[str, Any]], dfs: dict[str, pd.DataFrame]
) -> list[dict[str, Any]]:
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
            "pathogens": [
                "VIOLIN_c_pathogen_id",
                "Pathogen",
                "NCBI_Taxonomy_ID",
                "Disease",
                "Pathogen_Description",
                "Microbial_Pathogenesis",
                "Host_Ranges_and_Animal_Models",
                "Host_Protective_Immunity",
            ],
            "bvbrc_genomes": [
                "Genome ID",
                "Genome Name",
                "NCBI Taxon ID",
                "Family",
                "Genus",
                "Species",
                "Strain",
                "Isolation Country",
                "Geographic Location",
                "Host Name",
                "Host Common Name",
            ],
        },
        "vaccine": {
            "vaccines": [
                "Vaccine",
                "Vaccine_Name",
                "Vaccine_Ontology_ID",
                "Type",
                "Status",
                "Host_Species_as_Laboratory_Animal_Model",
                "Immunization_Route",
                "Antigen",
                "Manufacturer",
                "Location_Licensed",
                "Description",
                "Preparation",
                "Virulence",
            ]
        },
        "gene": {
            "genes": [
                "Gene_Name",
                "Organism",
                "VIOLIN_c_gene_id",
                "NCBI_Gene_ID",
                "Protein_Name",
                "Molecule_Role",
                "Locus_Tag",
                "Genbank_Accession",
            ]
        },
        "genome": {
            "bvbrc_genomes": [
                "Genome ID",
                "Genome Name",
                "Other Names",
                "NCBI Taxon ID",
                "Taxon Lineage Names",
                "Family",
                "Genus",
                "Species",
                "Strain",
                "GenBank Accessions",
                "Size",
                "GC Content",
                "CDS",
                "Isolation Country",
                "Geographic Location",
                "Host Name",
                "Host Common Name",
                "Collection Date",
                "Collection Year",
            ]
        },
    }

    # Function to find a term in a dataframe
    def find_term_in_df(term, df, columns_to_search):
        """Find rows where the term appears in any of the specified columns."""
        mask = pd.Series(False, index=df.index)
        for col in columns_to_search:
            if col in df.columns:
                # Handle NaN values and convert to string for comparison
                col_mask = (
                    df[col].fillna("").astype(str).str.contains(term, case=False, regex=False)
                )
                mask = mask | col_mask
        return df[mask]

    # For each match, find relevant data in the database
    enriched_matches = []

    for match in matches:
        query_entity = match["query_entity"]
        synonym = match["synonym"]
        entity_type = next((e["type"] for e in extract_entities_llm(query_entity)), None)

        relevant_data = {}

        # Find relevant data in each applicable table based on entity type
        if entity_type in important_fields:
            for table_name, fields in important_fields[entity_type].items():
                if table_name in dfs:
                    # Search for the synonym in text columns
                    text_columns = [
                        col
                        for col in dfs[table_name].columns
                        if dfs[table_name][col].dtype == "object"
                    ]
                    relevant_rows = find_term_in_df(synonym, dfs[table_name], text_columns)

                    if not relevant_rows.empty:
                        # Extract only important fields for this entity type
                        available_fields = [f for f in fields if f in relevant_rows.columns]
                        data = relevant_rows[available_fields].iloc[0].to_dict()
                        relevant_data[table_name] = data

        # For pathogens and vaccines, also look for related genes
        if (
            entity_type in ["pathogen", "vaccine"]
            and "vaccine_pathogen" in dfs
            and "gene_vaccine_pathogen" in dfs
        ):
            # First, find vaccine-pathogen relationships
            vp_df = dfs["vaccine_pathogen"]
            gene_vp_df = dfs["gene_vaccine_pathogen"]
            genes_df = dfs.get("genes", pd.DataFrame())

            # Find pathogen or vaccine IDs
            pathogen_id = None
            vaccine_id = None

            # Try to extract IDs from relevant data
            if "pathogens" in relevant_data and entity_type == "pathogen":
                pathogen_id = relevant_data.get("pathogens", {}).get("VIOLIN_c_pathogen_id")
            elif "vaccines" in relevant_data and entity_type == "vaccine":
                vaccine_id = relevant_data.get("vaccines", {}).get("id")

            # Find vaccine-pathogen relationships
            vp_matches = pd.DataFrame()
            if pathogen_id is not None and "VIOLIN_c_pathogen_id" in vp_df.columns:
                vp_matches = vp_df[vp_df["VIOLIN_c_pathogen_id"] == pathogen_id]
            elif vaccine_id is not None and "vaccine_id" in vp_df.columns:
                vp_matches = vp_df[vp_df["vaccine_id"] == vaccine_id]

            # If we found any vaccine-pathogen relationships, look for associated genes
            if not vp_matches.empty and not gene_vp_df.empty and not genes_df.empty:
                vp_ids = vp_matches["id"].tolist() if "id" in vp_matches.columns else []

                # Find genes associated with these vaccine-pathogen pairs
                gene_matches = (
                    gene_vp_df[gene_vp_df["vaccine_pathogen_id"].isin(vp_ids)]
                    if "vaccine_pathogen_id" in gene_vp_df.columns
                    else pd.DataFrame()
                )

                # Get gene details
                if not gene_matches.empty and "gene_id" in gene_matches.columns:
                    gene_ids = gene_matches["gene_id"].tolist()
                    gene_details = (
                        genes_df[genes_df["id"].isin(gene_ids)]
                        if "id" in genes_df.columns
                        else pd.DataFrame()
                    )

                    if not gene_details.empty:
                        # Select important gene fields
                        gene_fields = important_fields.get("gene", {}).get("genes", [])
                        available_gene_fields = [
                            f for f in gene_fields if f in gene_details.columns
                        ]

                        # Add gene information
                        relevant_data["related_genes"] = gene_details[
                            available_gene_fields
                        ].to_dict("records")

        # Add the relevant data to the match
        enriched_match = match.copy()
        if relevant_data:
            enriched_match["relevant_data"] = relevant_data

        enriched_matches.append(enriched_match)

    return enriched_matches


# G79 (2026-05-16): the CSV-agent shell (dormant code paths that
# wrap LangChain's create_csv_agent) lives in _csv_agent_shell.py.
# Re-exported here so existing
# ``from apecx_integration.agents.violin_bvbrc.agent import X``
# imports keep working without change. The noqa is required because
# this import sits at the END of the module body — agent.py is the
# partial-execution parent in the circular import sequence.
from apecx_integration.agents.violin_bvbrc._csv_agent_shell import (  # noqa: E402
    main,
)

if __name__ == "__main__":
    main()
