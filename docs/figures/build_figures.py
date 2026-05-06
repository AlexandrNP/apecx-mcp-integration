"""Generate architecture diagram images for docs + slides.

Run: ``PYTHONPATH=src .venv/bin/python docs/figures/build_figures.py``

Produces seaborn-styled PNGs (alpha=0.7 fills) sized for slide use:

    docs/figures/01_three_tier_topology.png
    docs/figures/02_synthesis_pipeline.png
    docs/figures/03_invocation_paths.png
    docs/figures/04_resolution_decision_tree.png
    docs/figures/05_test_surface.png
    docs/figures/06_ontologies_coverage.png
    docs/figures/07_mcp_tool_distribution.png
    docs/figures/08_trigger_cascade_timeline.png
    docs/figures/09_session_bug_count.png

Design rules (from the user's 2026-05-05 ask):

  - seaborn-based visualizations (the styling, not necessarily the
    chart type — architectural diagrams are matplotlib boxes/arrows
    with seaborn-aligned palette and typography).
  - alpha=0.7 on every color fill.
  - Legible, non-overlapping labels — every annotation has a
    bounding box big enough to fit the longest label, and arrows
    are routed clear of text.
  - No unnecessary visual elements (no decorative grids on
    architectural diagrams; no chart titles that duplicate the
    figure caption).
  - Presentation-suitable fonts — Helvetica/Arial fallback chain.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

OUT_DIR = Path(__file__).parent
FIG_DPI = 200
ALPHA = 0.7

# Use seaborn's whitegrid for chart-style figures and a clean white
# background for architecture-style figures. Set the global font.
sns.set_theme(style="white", font="Helvetica", font_scale=1.0)
plt.rcParams.update(
    {
        "font.family": ["Helvetica", "Arial", "DejaVu Sans"],
        "axes.spines.top": False,
        "axes.spines.right": False,
        "savefig.bbox": "tight",
        "savefig.dpi": FIG_DPI,
    }
)


def _box(ax, x, y, w, h, text, color, fontsize=10, fontweight="normal"):
    """Draw a rounded box with alpha=0.7 fill and a centered label."""
    box = FancyBboxPatch(
        (x, y),
        w,
        h,
        boxstyle="round,pad=0.02,rounding_size=0.04",
        linewidth=1.0,
        edgecolor=color,
        facecolor=color,
        alpha=ALPHA,
    )
    ax.add_patch(box)
    ax.text(
        x + w / 2,
        y + h / 2,
        text,
        ha="center",
        va="center",
        fontsize=fontsize,
        fontweight=fontweight,
        wrap=True,
    )


def _arrow(ax, x1, y1, x2, y2, color="#444", lw=1.4, style="-|>"):
    """Solid directed arrow."""
    arr = FancyArrowPatch(
        (x1, y1),
        (x2, y2),
        arrowstyle=style,
        mutation_scale=12,
        linewidth=lw,
        color=color,
        alpha=ALPHA,
    )
    ax.add_patch(arr)


# ---------------------------------------------------------------------
# Figure 1 — Three-tier runtime topology
# ---------------------------------------------------------------------


def fig_three_tier_topology() -> None:
    """Cleaner 3-tier layout: tiers stacked vertically with explicit
    horizontal lanes; arrows routed in the gutters between tiers (never
    through boxes); each tool group on its own row with full label."""
    fig, ax = plt.subplots(figsize=(15, 9.5))
    palette = sns.color_palette("muted", n_colors=8)

    # Y bands (top → bottom): client (8.5), server (7.7), tools (5.4–7.0),
    # tier 2/4 (3.5–4.4), data sources (1.0–2.0)
    # Tier labels (left margin)
    label_x = 0.2
    ax.text(label_x, 8.7, "MCP CLIENT", fontsize=10, fontweight="bold", color="#666")
    ax.text(
        label_x,
        7.95,
        "TIER 1 — MCP SURFACE (THIS REPO)",
        fontsize=10,
        fontweight="bold",
        color="#666",
    )
    ax.text(
        label_x,
        4.7,
        "TIER 2 / TIER 4 — CONTROL PLANE & EXECUTORS",
        fontsize=10,
        fontweight="bold",
        color="#666",
    )
    ax.text(
        label_x,
        2.4,
        "DATA SOURCES & EXTERNAL SERVICES",
        fontsize=10,
        fontweight="bold",
        color="#666",
    )

    # MCP client (top, centered)
    _box(ax, 5.5, 8.4, 4.0, 0.6, "Claude Desktop / IDE / CLI  (stdio)", palette[0], fontsize=11)

    # FastMCP server
    _box(ax, 4.5, 7.4, 6.0, 0.6, "FastMCP server  —  23 tools", palette[1], 11.5, "bold")

    # Tool groups arranged in a 4×2 grid (each cell wide enough for a 2-line label)
    GROUP_W, GROUP_H = 3.4, 0.85
    GROUP_X0, GROUP_Y0 = 0.4, 6.3
    GROUP_DX, GROUP_DY = 3.6, 1.0
    tool_groups = [
        # (col, row, label, color)
        (0, 0, "workflow tools  (3)\nstart_workflow / show_diff / execute", palette[2]),
        (1, 0, "discovery tools  (2)\nlist_workflows / describe_workflow", palette[2]),
        (2, 0, "database tools  (7)\nquery_vaccines / pathogens / genes / …", palette[2]),
        (3, 0, "approval tools  (4)\nlist / approve / reject / correct", palette[2]),
        (0, 1, "resolve_canonical_entity  (1)", palette[3]),
        (1, 1, "synthesize_query  (1)", palette[3]),
        (2, 1, "query_globus_search  (1)", palette[3]),
        (3, 1, "HPC tools  (4)\nestimate / confirm / export / ingest", palette[2]),
    ]
    for col, row, text, c in tool_groups:
        x = GROUP_X0 + col * GROUP_DX
        y = GROUP_Y0 - row * GROUP_DY
        _box(ax, x, y, GROUP_W, GROUP_H, text, c, fontsize=9.5)

    # Tier 2 / Tier 4
    _box(
        ax,
        1.0,
        3.7,
        5.5,
        0.85,
        "Control Plane (apecx-cp serve)\nFastAPI · SQLite · workflows · runs · approvals · artifacts",
        palette[4],
        10,
    )
    _box(
        ax,
        8.0,
        3.7,
        5.5,
        0.85,
        "Executors (nanobrain)\nLocalExecutor · Parsl · Academy — drive Workflow.process()",
        palette[5],
        10,
    )

    # Data sources — single row, well spaced
    DATA_W, DATA_H = 2.0, 0.85
    sources = [
        (0.3, "domain_rag\nFAISS + sentence-tx"),
        (2.5, "VIOLIN CSVs\ndata/violin/"),
        (4.7, "BV-BRC TSVs\ndata/bvbrc_cache/"),
        (6.9, "PubMed eUtils\n(network)"),
        (9.1, "Globus Search\n(network, public)"),
        (11.3, "synonym_dict\nSQLite"),
        (13.5, "LLM API\n(env-driven)"),
    ]
    for x, lbl in sources[:-1]:
        _box(ax, x, 1.05, DATA_W, DATA_H, lbl, palette[6], 9)
    # LLM API distinguished
    x, lbl = sources[-1]
    _box(ax, x, 1.05, DATA_W, DATA_H, lbl, palette[7], 9)

    # Arrows — routed in gutters, never through boxes
    # Client → server
    _arrow(ax, 7.5, 8.4, 7.5, 8.0)
    # Server → tool zone (one arrow at a representative tool)
    _arrow(ax, 7.5, 7.4, 7.5, 7.15)
    # Workflow tools → control plane (left side)
    _arrow(ax, 2.1, 6.3, 3.0, 4.55, color="#3060a0", lw=1.5)
    # HPC tools → control plane
    _arrow(ax, 12.6, 5.3, 5.5, 4.55, color="#3060a0", lw=1.5, style="-|>")
    # Synthesize_query → Globus + LLM (long path on right)
    _arrow(ax, 5.5, 5.3, 10.1, 1.9, color="#a04040", lw=1.5)
    _arrow(ax, 5.5, 5.3, 14.5, 1.9, color="#a04040", lw=1.5)
    # query_globus_search → globus
    _arrow(ax, 9.1, 5.3, 10.1, 1.9, color="#a04040", lw=1.5)
    # Control plane → executors (lateral)
    _arrow(ax, 6.5, 4.12, 8.0, 4.12, color="#446080", lw=1.4, style="<|-|>")
    # Executors → data sources (LLM end)
    _arrow(ax, 13.0, 3.7, 14.5, 1.9, color="#446080", lw=1.4)

    ax.set_xlim(0, 16.0)
    ax.set_ylim(0.5, 9.4)
    ax.set_aspect("auto")
    ax.axis("off")
    fig.savefig(OUT_DIR / "01_three_tier_topology.png")
    plt.close(fig)


# ---------------------------------------------------------------------
# Figure 2 — Synthesis pipeline data flow (5 retrieval branches)
# ---------------------------------------------------------------------


def fig_synthesis_pipeline() -> None:
    """Two-step pipeline drawn left-to-right with a clearly separated
    branch column in the middle. No arrows pass through any box; the
    bundle is shown as a horizontal slab connecting assembly to
    synthesis."""
    fig, ax = plt.subplots(figsize=(15, 8.5))
    palette = sns.color_palette("Set2", n_colors=6)

    # Input
    _box(ax, 0.2, 4.5, 1.5, 0.8, "scientist\nquery", palette[0], 10.5, "bold")

    # ---------- Assembly step (containing border) ----------
    _box(ax, 2.0, 1.4, 7.5, 6.4, "", palette[1], 10)
    ax.text(5.75, 7.45, "SynthesisContextAssemblyStep", ha="center", fontsize=13, fontweight="bold")

    # gather hub (left side of assembly box)
    _box(ax, 2.3, 4.5, 2.2, 0.8, "asyncio.gather\nreturn_exceptions=True", palette[2], 10, "bold")

    # 4 retrieval branch boxes (stacked, right side of assembly box)
    branches = [
        (6.4, "DomainRagIndex.search\n(FAISS, in-memory)", palette[3]),
        (5.3, "lookup_violin / lookup_bvbrc\n(pandas, offline)", palette[3]),
        (4.2, "PubMed eSearch + eFetch\n(network, optional)", palette[3]),
        (3.1, "Globus Search\n(network, public index)", palette[3]),
    ]
    for y, lbl, c in branches:
        _box(ax, 5.4, y, 4.0, 0.85, lbl, c, 9.5)

    # Bundle (bottom of assembly box, full width)
    _box(
        ax,
        2.3,
        1.65,
        7.0,
        0.8,
        "synthesis_bundle_output\n{query, rag_chunks, bvbrc_genomes, violin_mappings, publications, globus_results}",
        palette[4],
        8.5,
        "bold",
    )

    # ---------- Synthesis step ----------
    _box(ax, 10.2, 1.4, 4.5, 6.4, "", palette[5], 10)
    ax.text(12.45, 7.45, "RagSynthesisStep", ha="center", fontsize=13, fontweight="bold")
    _box(
        ax,
        10.4,
        5.6,
        4.1,
        0.95,
        "synthesize_response\n(single LLM round-trip)",
        palette[3],
        10,
        "bold",
    )
    _box(
        ax,
        10.4,
        4.2,
        4.1,
        0.95,
        "validation gates\nsize · grounded · empty-retrieval",
        palette[3],
        9.5,
    )
    _box(
        ax,
        10.4,
        2.7,
        4.1,
        0.95,
        "synthesis_output\n{synthesis: <markdown>}",
        palette[4],
        9.5,
        "bold",
    )
    _box(ax, 10.4, 1.55, 4.1, 0.7, "Markdown answer\nwith inline citations", palette[0], 10, "bold")

    # ---------- Arrows ----------
    # Query → gather
    _arrow(ax, 1.7, 4.9, 2.3, 4.9)
    # gather → each branch (4 short arrows)
    for y, _, _ in branches:
        _arrow(ax, 4.5, 4.9, 5.4, y + 0.4)
    # branches → bundle (one arrow per branch, routed left to bundle)
    for y, _, _ in branches:
        _arrow(ax, 5.4, y + 0.4, 5.0, 2.45, color="#666", lw=1.0)
    # bundle → synthesize_response (cross from assembly box into synthesis box)
    _arrow(ax, 9.3, 2.05, 10.4, 6.0, color="#306060", lw=1.8)
    # within synthesis box: synthesize → gates → output → markdown
    _arrow(ax, 12.45, 5.6, 12.45, 5.15)
    _arrow(ax, 12.45, 4.2, 12.45, 3.65)
    _arrow(ax, 12.45, 2.7, 12.45, 2.25)

    ax.set_xlim(0, 15.5)
    ax.set_ylim(1.0, 8.0)
    ax.axis("off")
    fig.savefig(OUT_DIR / "02_synthesis_pipeline.png")
    plt.close(fig)


# ---------------------------------------------------------------------
# Figure 3 — Three invocation paths
# ---------------------------------------------------------------------


def fig_invocation_paths() -> None:
    fig, ax = plt.subplots(figsize=(13, 6.5))
    palette = sns.color_palette("pastel", n_colors=6)

    ax.text(
        0.5,
        5.8,
        "Path A — synthesize_query MCP tool (canonical operator path)",
        fontsize=11.5,
        fontweight="bold",
    )
    ax.text(
        0.5,
        3.95,
        "Path B — Workflow runtime (composer planning + trigger cascade)",
        fontsize=11.5,
        fontweight="bold",
    )
    ax.text(
        0.5,
        2.05,
        "Path C — Direct step instantiation (test code only)",
        fontsize=11.5,
        fontweight="bold",
    )

    # Path A
    _box(ax, 0.5, 4.95, 1.8, 0.65, "scientist query", palette[0], 9.5)
    _box(
        ax,
        2.7,
        4.95,
        2.5,
        0.65,
        "synthesize_query MCP tool\n(cached singletons)",
        palette[1],
        9,
        "bold",
    )
    _box(ax, 5.6, 4.95, 2.5, 0.65, "assembly.process()\n→ synthesis.process()", palette[2], 9.5)
    _box(
        ax,
        8.5,
        4.95,
        4.0,
        0.65,
        "{synthesis: markdown, retrieved: {counts}}",
        palette[3],
        9,
        "bold",
    )
    _arrow(ax, 2.3, 5.27, 2.7, 5.27)
    _arrow(ax, 5.2, 5.27, 5.6, 5.27)
    _arrow(ax, 8.1, 5.27, 8.5, 5.27)

    # Path B
    _box(ax, 0.5, 3.1, 1.8, 0.65, "scientist query", palette[0], 9.5)
    _box(ax, 2.7, 3.1, 2.5, 0.65, "Workflow.from_config\n→ initialize() → process()", palette[1], 9)
    _box(ax, 5.6, 3.1, 2.5, 0.65, "trigger cascade\n(async background)", palette[2], 9.5)
    _box(ax, 8.5, 3.1, 2.0, 0.65, "wait_for_cascade(\nawaits drain)", palette[2], 9)
    _box(ax, 10.7, 3.1, 1.8, 0.65, "synthesis_output\n.get()", palette[3], 9, "bold")
    _arrow(ax, 2.3, 3.42, 2.7, 3.42)
    _arrow(ax, 5.2, 3.42, 5.6, 3.42)
    _arrow(ax, 8.1, 3.42, 8.5, 3.42, color="#a04040", lw=1.6, style="->")
    _arrow(ax, 10.5, 3.42, 10.7, 3.42)

    # Path C
    _box(ax, 0.5, 1.2, 1.8, 0.65, "scientist query", palette[0], 9.5)
    _box(ax, 2.7, 1.2, 2.5, 0.65, "BaseStep.from_config\n(per step)", palette[1], 9)
    _box(ax, 5.6, 1.2, 2.5, 0.65, "step_a.process()\n→ step_b.process()", palette[2], 9.5)
    _box(ax, 8.5, 1.2, 4.0, 0.65, "{synthesis: markdown}", palette[3], 9, "bold")
    _arrow(ax, 2.3, 1.52, 2.7, 1.52)
    _arrow(ax, 5.2, 1.52, 5.6, 1.52)
    _arrow(ax, 8.1, 1.52, 8.5, 1.52)

    # Bottom note
    ax.text(
        0.5,
        0.3,
        "Path A: 1 LLM call · ~30s · MCP-friendly · cached step instances\n"
        "Path B: 1 LLM call · ~70s with cascade primitive · exercises framework triggers/links\n"
        "Path C: 1 LLM call · ~30s · bypasses framework · used by integration tests only",
        fontsize=9,
        color="#444",
    )

    ax.set_xlim(0, 13.5)
    ax.set_ylim(0, 6.3)
    ax.axis("off")
    fig.savefig(OUT_DIR / "03_invocation_paths.png")
    plt.close(fig)


# ---------------------------------------------------------------------
# Figure 4 — Resolution decision tree (fast / ancestor / slow / miss)
# ---------------------------------------------------------------------


def fig_resolution_decision_tree() -> None:
    fig, ax = plt.subplots(figsize=(11, 7.5))
    palette = sns.color_palette("Set2", n_colors=5)

    # Top: input
    _box(ax, 4.0, 6.2, 3.0, 0.7, "surface form + entity_type?", palette[0], 11, "bold")

    # Normalize
    _box(
        ax,
        4.0,
        5.0,
        3.0,
        0.6,
        "normalize_surface_form\n(lower / strip / squash whitespace)",
        palette[1],
        9,
    )
    _arrow(ax, 5.5, 6.2, 5.5, 5.6)

    # Decision: fast lookup
    _box(ax, 4.0, 3.7, 3.0, 0.7, "inverse_index lookup\nO(1) hash", palette[2], 10, "bold")
    _arrow(ax, 5.5, 5.0, 5.5, 4.4)

    # Hit branch
    _box(ax, 8.0, 3.7, 2.5, 0.7, "DictionaryEntry\npath=fast", palette[3], 10, "bold")
    _arrow(ax, 7.0, 4.05, 8.0, 4.05, color="#306030", lw=1.6)
    ax.text(7.5, 4.25, "hit", fontsize=9, color="#306030")

    # Miss branch (NCBITaxon IRI)
    _box(ax, 0.5, 2.5, 3.0, 0.7, "taxon_hierarchy\nrecursive CTE upward", palette[2], 9.5, "bold")
    _arrow(ax, 4.0, 3.7, 2.0, 3.2, color="#a04040", lw=1.5)
    ax.text(2.4, 3.5, "miss + NCBITaxon IRI", fontsize=8.5, color="#a04040")

    _box(ax, 0.5, 1.3, 3.0, 0.6, "DictionaryEntry\npath=ancestor (× 0.9)", palette[3], 9.5, "bold")
    _arrow(ax, 2.0, 2.5, 2.0, 1.9, color="#306030")

    # Slow path (subset)
    _box(ax, 4.0, 2.5, 3.0, 0.7, "DatabaseStore\nsubstring scan", palette[2], 9.5, "bold")
    _arrow(ax, 5.5, 3.7, 5.5, 3.2, color="#a04040", lw=1.5)
    ax.text(5.7, 3.45, "miss + non-IRI", fontsize=8.5, color="#a04040")

    _box(ax, 4.0, 1.3, 3.0, 0.6, "LookupResult\npath=slow (~0.3)", palette[3], 9.5, "bold")
    _arrow(ax, 5.5, 2.5, 5.5, 1.9)

    # Miss
    _box(ax, 8.0, 1.3, 2.5, 0.6, "LookupResult\npath=miss (0.0)", palette[4], 9.5, "bold")
    _arrow(ax, 7.0, 2.85, 9.25, 1.9, color="#a04040", lw=1.5)
    ax.text(7.6, 2.4, "no match", fontsize=8.5, color="#a04040")

    # Footer
    ax.text(
        0.5,
        0.4,
        "Visibility guarantee: every tool returns (path, confidence). Silent routing is forbidden.",
        fontsize=9.5,
        style="italic",
        color="#444",
    )

    ax.set_xlim(0, 11)
    ax.set_ylim(0, 7.3)
    ax.axis("off")
    fig.savefig(OUT_DIR / "04_resolution_decision_tree.png")
    plt.close(fig)


# ---------------------------------------------------------------------
# Figure 5 — Test surface (stacked bar, seaborn)
# ---------------------------------------------------------------------


def fig_test_surface() -> None:
    fig, ax = plt.subplots(figsize=(11, 6))
    palette = sns.color_palette("muted", n_colors=4)

    categories = [
        "Synthesis branch failures",
        "synthesize_query MCP tool",
        "VIOLIN/BV-BRC helpers",
        "PubMed helpers",
        "Globus Search",
        "Composer prompt correctness",
        "Workspace root resolver",
        "Descendant traversal (NCBITaxon)",
        "Workflow YAML loadability",
        "E2E pipeline (Ollama-gated)",
    ]
    counts = [6, 7, 12, 26, 19, 7, 6, 9, 7, 25]
    is_runtime = [False, False, False, False, False, False, False, False, True, True]

    colors = [palette[2] if rt else palette[0] for rt in is_runtime]
    y_pos = np.arange(len(categories))

    bars = ax.barh(y_pos, counts, color=colors, alpha=ALPHA, edgecolor="#333", linewidth=0.6)

    for bar, n in zip(bars, counts, strict=True):
        ax.text(
            bar.get_width() + 0.4,
            bar.get_y() + bar.get_height() / 2,
            str(n),
            va="center",
            fontsize=10,
            fontweight="bold",
        )

    ax.set_yticks(y_pos)
    ax.set_yticklabels(categories, fontsize=10)
    ax.set_xlabel("Test count", fontsize=11)
    ax.invert_yaxis()
    ax.set_xlim(0, max(counts) + 6)
    sns.despine(left=True, bottom=False)

    # Legend placed ABOVE the chart so it never overlaps the longest
    # bar's value label.
    leg_handles = [
        mpatches.Patch(color=palette[0], alpha=ALPHA, label="hermetic (no external deps)"),
        mpatches.Patch(
            color=palette[2], alpha=ALPHA, label="runtime (gated on Ollama / FAISS / VIOLIN)"
        ),
    ]
    ax.legend(
        handles=leg_handles,
        loc="lower center",
        bbox_to_anchor=(0.5, 1.02),
        ncol=2,
        frameon=False,
        fontsize=10,
    )

    fig.savefig(OUT_DIR / "05_test_surface.png")
    plt.close(fig)


# ---------------------------------------------------------------------
# Figure 6 — Ontologies coverage
# ---------------------------------------------------------------------


def fig_ontologies_coverage() -> None:
    """Wider canvas so the Hierarchy column (longest cell text:
    "ancestor + descendant") never overlaps the Used-by column."""
    fig, ax = plt.subplots(figsize=(14, 6.0))
    palette = sns.color_palette("deep", n_colors=6)

    ontologies = ["NCBITaxon", "VO", "DOID", "GO", "NCBIGene", "APECx Local"]
    iri_prefixes = [
        "obo/NCBITaxon_",
        "obo/VO_",
        "obo/DOID_",
        "obo/GO_",
        "identifiers.org/ncbigene/",
        "apecx.local/",
    ]
    has_hierarchy = [True, False, False, False, False, False]
    used_in = [
        "pathogen, genome",
        "vaccine",
        "disease (reserved)",
        "gene/protein function",
        "gene",
        "lab strain (private)",
    ]

    y_pos = np.arange(len(ontologies))[::-1]

    # Column x-anchors with generous gaps so the longest cell text in
    # any column doesn't overlap the next column.
    COL_NAME = 0.0  # ontology name box
    COL_IRI = 3.2  # IRI prefix start
    COL_HIER = 7.5  # hierarchy marker start
    COL_USE = 11.4  # usage start

    for i, (ont, pfx, hier, usage) in enumerate(
        zip(ontologies, iri_prefixes, has_hierarchy, used_in, strict=True)
    ):
        y = y_pos[i]
        _box(ax, COL_NAME, y - 0.32, 2.8, 0.65, ont, palette[i], 12, "bold")
        ax.text(COL_IRI, y, pfx, va="center", fontsize=11, family="monospace")
        marker = "✓ ancestor + descendant" if hier else "—"
        marker_color = "#2a662a" if hier else "#a04040"
        ax.text(
            COL_HIER, y, marker, va="center", fontsize=11, fontweight="bold", color=marker_color
        )
        ax.text(COL_USE, y, usage, va="center", fontsize=11, color="#444")

    # Headers
    HEADER_Y = len(ontologies) + 0.0
    ax.text(COL_NAME + 1.4, HEADER_Y, "Ontology", ha="center", fontsize=12, fontweight="bold")
    ax.text(COL_IRI, HEADER_Y, "IRI prefix", fontsize=12, fontweight="bold")
    ax.text(COL_HIER, HEADER_Y, "Hierarchy", fontsize=12, fontweight="bold")
    ax.text(COL_USE, HEADER_Y, "Used by", fontsize=12, fontweight="bold")

    ax.set_xlim(-0.3, 16.5)
    ax.set_ylim(-0.7, len(ontologies) + 0.7)
    ax.axis("off")
    fig.savefig(OUT_DIR / "06_ontologies_coverage.png")
    plt.close(fig)


# ---------------------------------------------------------------------
# Figure 7 — MCP tool distribution (bar chart)
# ---------------------------------------------------------------------


def fig_mcp_tool_distribution() -> None:
    fig, ax = plt.subplots(figsize=(11, 5.5))

    tool_groups = [
        ("Workflow", 3),
        ("Discovery", 2),
        ("Database", 7),
        ("Entity resolution", 1),
        ("Synthesis", 1),
        ("Globus Search", 1),
        ("Approval (HITL)", 4),
        ("HPC bundle", 4),
    ]
    labels = [g[0] for g in tool_groups]
    counts = [g[1] for g in tool_groups]
    colors = sns.color_palette("muted", n_colors=len(tool_groups))

    x = np.arange(len(labels))
    bars = ax.bar(x, counts, color=colors, alpha=ALPHA, edgecolor="#333", linewidth=0.6)
    for bar, n in zip(bars, counts, strict=True):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + 0.08,
            str(n),
            ha="center",
            fontsize=11,
            fontweight="bold",
        )

    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=10, rotation=20, ha="right")
    ax.set_ylabel("Number of tools", fontsize=11)
    ax.set_ylim(0, max(counts) + 1.5)
    ax.set_title("MCP surface — 23 tools across 8 groups", fontsize=12, pad=10)
    sns.despine(top=True, right=True)
    fig.savefig(OUT_DIR / "07_mcp_tool_distribution.png")
    plt.close(fig)


# ---------------------------------------------------------------------
# Figure 8 — Trigger cascade timeline
# ---------------------------------------------------------------------


def fig_trigger_cascade_timeline() -> None:
    """Vertical step list with non-overlapping labels (the prior
    horizontal layout crushed adjacent labels). Each event has its own
    full-width row; arrow connects rows in reading order."""
    fig, ax = plt.subplots(figsize=(13, 9.0))
    palette = sns.color_palette("muted", n_colors=7)

    events = [
        (
            "Workflow.from_config('rag_e2e_synthesis_workflow.yml')",
            "Step instances constructed; triggers NOT yet resolved.",
            palette[0],
        ),
        (
            "await wf.initialize()",
            "Phase 3: triggers resolve, listeners register on data units.",
            palette[1],
        ),
        (
            "await wf.process({'assembly_input': {'query': '...'}})",
            "Writes to first step's input data unit; returns {data_flow_initiated}.",
            palette[2],
        ),
        (
            "DataUnitChangeTrigger fires assembly.process()",
            "FAISS load + 4 concurrent retrieval branches (asyncio.gather).",
            palette[3],
        ),
        (
            "Single-output fallback writes synthesis_bundle_output",
            "Step returned {query, rag_chunks, …}; framework writes whole dict.",
            palette[4],
        ),
        (
            "DirectLink (auto_transfer=true) transfers to synthesis_input",
            "Source data unit change triggers the link; target data unit set.",
            palette[5],
        ),
        (
            "synthesis.process() — single LLM round-trip + validation gates",
            "fail_on_empty_retrieval, size, and grounded-citation gates fire.",
            palette[6],
        ),
        (
            "await wf.wait_for_cascade(timeout=90, settle_ms=100) returns True",
            "Trigger executor's task set drained + stayed empty for the settle window.",
            palette[2],
        ),
        (
            "synthesis_output data unit holds the Markdown answer",
            "Test reads via synthesis.step_output_data_units['synthesis_output'].get().",
            palette[3],
        ),
    ]

    n = len(events)
    row_h = 0.85
    box_w = 9.5
    box_x = 1.6

    # Title
    ax.text(
        0.5,
        n * row_h + 1.2,
        "Workflow.wait_for_cascade — call sequence",
        fontsize=14,
        fontweight="bold",
    )
    ax.text(
        0.5,
        n * row_h + 0.7,
        "added 2026-05-05 to nanobrain. Polls the trigger executor's task set with a settle window;",
        fontsize=10,
        color="#444",
    )
    ax.text(
        0.5,
        n * row_h + 0.35,
        "handles transitively-spawned tasks (a single snapshot would miss cascade-spawned tasks).",
        fontsize=10,
        color="#444",
    )

    # Step indices on the left, boxes in the middle
    for i, (head, sub, color) in enumerate(events):
        y = (n - i - 1) * row_h
        # Step number circle
        ax.text(
            0.7,
            y + row_h / 2,
            f"{i + 1}.",
            fontsize=14,
            fontweight="bold",
            color="#444",
            ha="center",
            va="center",
        )
        # Event box
        _box(ax, box_x, y + 0.05, box_w, row_h - 0.15, "", color, fontsize=10)
        # Headline + sublabel inside the box
        ax.text(
            box_x + 0.2,
            y + row_h - 0.25,
            head,
            fontsize=11,
            fontweight="bold",
            va="center",
            family="monospace",
        )
        ax.text(box_x + 0.2, y + 0.3, sub, fontsize=10, color="#333", va="center")

    # Vertical arrow on the left connecting each box to the next
    for i in range(n - 1):
        y_top = (n - i - 1) * row_h + 0.05
        y_bot = (n - i - 2) * row_h + row_h - 0.10
        _arrow(ax, 1.2, y_top, 1.2, y_bot, color="#999", lw=1.2)

    ax.set_xlim(0, 12.0)
    ax.set_ylim(-0.2, n * row_h + 1.6)
    ax.axis("off")
    fig.savefig(OUT_DIR / "08_trigger_cascade_timeline.png")
    plt.close(fig)


# ---------------------------------------------------------------------
# Figure 9 — Session bug count by class
# ---------------------------------------------------------------------


def fig_session_bug_count() -> None:
    fig, ax = plt.subplots(figsize=(11, 5.5))
    palette = sns.color_palette("muted", n_colors=4)

    bug_classes = [
        "Silent failure\n(YAML loads, no work done)",
        "Compliance\n(extra='forbid' missing)",
        "Architectural debt\n(object.__new__)",
        "Documentation drift\n(stale tool count, missing branch)",
    ]
    counts = [4, 1, 2, 5]
    found_by = [
        "trigger cascade test (this session)",
        "earlier audit",
        "earlier audit + this session",
        "this session",
    ]

    x = np.arange(len(bug_classes))
    bars = ax.bar(x, counts, color=palette, alpha=ALPHA, edgecolor="#333", linewidth=0.6)
    for bar, n, fb in zip(bars, counts, found_by, strict=True):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + 0.1,
            str(n),
            ha="center",
            fontsize=12,
            fontweight="bold",
        )
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            -0.6,
            fb,
            ha="center",
            fontsize=9,
            color="#444",
            style="italic",
        )

    ax.set_xticks(x)
    ax.set_xticklabels(bug_classes, fontsize=10)
    ax.set_ylabel("Bugs found / fixed this session", fontsize=11)
    ax.set_ylim(-1.2, max(counts) + 1.2)
    ax.set_title(
        "12 bugs / drifts uncovered across this session — by class",
        fontsize=12,
        pad=12,
    )
    sns.despine(top=True, right=True)
    fig.savefig(OUT_DIR / "09_session_bug_count.png")
    plt.close(fig)


# ---------------------------------------------------------------------
# Figure 10 — Backend harmonization pipeline (offline, run periodically)
# ---------------------------------------------------------------------


def fig_backend_harmonization() -> None:
    """The OFFLINE pipeline that runs periodically to refresh the
    artifacts the user-facing workflow consumes. Two parallel
    sub-pipelines on top, full-width Resolution Status taxonomy on
    the bottom. Wider canvas + larger gutters so no labels overlap."""
    fig, ax = plt.subplots(figsize=(17, 11))
    palette = sns.color_palette("muted", n_colors=8)

    # Heading band
    _box(ax, 0.0, 10.4, 17.0, 0.6, "", palette[7], 11)
    ax.text(
        8.5,
        10.7,
        "BACKEND HARMONIZATION  ·  offline  ·  run periodically (e.g. monthly)",
        ha="center",
        fontsize=14,
        fontweight="bold",
        color="white",
    )

    # ============================================================
    # Top half — pipeline. Full width split into 3 columns:
    #   sources (left) → 2 stacked pipelines (middle) → artifacts (right)
    # ============================================================

    # Left: external sources
    ax.text(0.3, 9.95, "EXTERNAL SOURCES", fontsize=11, fontweight="bold", color="#666")
    sources = [
        "PubMed eUtils  (network)",
        "PDB  (network)",
        "DataCite  (network)",
        "Crossref / OpenAlex / bioRxiv",
        "EMDB / DOI  (network)",
        "NCBI taxdump  (one-time)",
        "VIOLIN source CSVs  (vendored)",
        "BV-BRC source TSVs  (vendored)",
    ]
    for i, s in enumerate(sources):
        _box(ax, 0.3, 9.2 - i * 0.6, 3.6, 0.5, s, palette[6], 9.5)

    # Middle-upper: harvester pipeline
    ax.text(
        4.4,
        9.95,
        "HARVESTER PIPELINE  (apecx-harvesters)",
        fontsize=11,
        fontweight="bold",
        color="#666",
    )
    ax.text(
        4.4,
        9.7,
        "OUT OF SCOPE — read-only consumer at the ingest seam",
        fontsize=8.5,
        color="#a04040",
        style="italic",
    )
    _box(
        ax,
        4.4,
        8.5,
        4.7,
        0.7,
        "loaders/<source>/  (9 loaders)\nsources.py  +  pipeline/run.py",
        palette[2],
        9.5,
        "bold",
    )
    _box(
        ax,
        4.4,
        7.5,
        4.7,
        0.7,
        "DataCite-shaped record normalization\n(per-source schema → unified container)",
        palette[2],
        9.5,
    )
    _box(
        ax,
        4.4,
        6.5,
        4.7,
        0.7,
        "scripts/aggregate_gsearch.py\nbatch ingestion → Globus Search API",
        palette[2],
        9.5,
        "bold",
    )
    _arrow(ax, 6.75, 8.5, 6.75, 8.2, lw=1.4)
    _arrow(ax, 6.75, 7.5, 6.75, 7.2, lw=1.4)

    # Middle-lower: dictionary builder
    ax.text(
        4.4,
        5.85,
        "DICTIONARY BUILDER  (apecx-mcp-integration)",
        fontsize=11,
        fontweight="bold",
        color="#666",
    )
    ax.text(4.4, 5.6, "Workflow: dictionary_build_workflow", fontsize=8.5, color="#666")
    _box(
        ax,
        4.4,
        4.4,
        4.7,
        0.7,
        "synonym_dictionary/build.py\nharvest from VIOLIN + BV-BRC source rows",
        palette[3],
        9.5,
        "bold",
    )
    _box(ax, 4.4, 3.4, 4.7, 0.7, "OLS resolution\n(EBI Ontology Lookup Service)", palette[3], 9.5)
    _box(
        ax,
        4.4,
        2.4,
        4.7,
        0.7,
        "synonym_dictionary/sqlite_writer.py\n+ taxon_hierarchy from NCBI taxdump",
        palette[3],
        9.5,
        "bold",
    )
    _arrow(ax, 6.75, 4.4, 6.75, 4.1, lw=1.4)
    _arrow(ax, 6.75, 3.4, 6.75, 3.1, lw=1.4)

    # Source → pipeline arrows (representative)
    _arrow(ax, 3.95, 8.5, 4.4, 8.85, color="#666", lw=1.0)
    _arrow(ax, 3.95, 4.5, 4.4, 4.75, color="#666", lw=1.0)

    # Right: artifacts
    ax.text(
        10.0,
        9.95,
        "ARTIFACTS  (consumed at query time)",
        fontsize=11,
        fontweight="bold",
        color="#666",
    )
    _box(
        ax,
        10.0,
        7.7,
        6.7,
        1.6,
        "Globus Search index\n\nsubject-keyed records of harvested\nPubMed / PDB / DataCite / ...\n\nUUID  e74bf12a-d0dd-4d19-a965-03f4936db851\npublic, no auth at query time",
        palette[4],
        9.5,
        "bold",
    )
    _box(
        ax,
        10.0,
        2.2,
        6.7,
        1.9,
        "apecx_synonym_dict.sqlite\n\n(entity_type, canonical_iri) → synonyms + ontology\n+ taxon_hierarchy table for ancestor/descendant CTEs\n+ ambiguous_surface_forms table\n\nenv:  APECX_SYNONYM_DICT_PATH",
        palette[5],
        9.5,
        "bold",
    )

    # Pipeline → artifact arrows
    _arrow(ax, 9.1, 6.85, 10.0, 7.9, color="#306060", lw=1.8)
    _arrow(ax, 9.1, 2.75, 10.0, 2.7, color="#604060", lw=1.8)

    # ============================================================
    # Bottom — Resolution Status Taxonomy as a 5-row stacked table
    # (descriptions are long; columnar layout would force wrap)
    # ============================================================
    _box(ax, 0.3, 0.0, 16.4, 2.1, "", palette[1], 9)
    ax.text(
        0.5,
        1.85,
        "RESOLUTION STATUS TAXONOMY  ·  per dictionary entry  ·  written by build.py, surfaced at query time as confidence",
        fontsize=11,
        fontweight="bold",
        color="#444",
    )

    statuses = [
        ("id_anchored", "1.0", "source row carried authoritative ID; OLS provided synonyms"),
        ("ols_exact", "0.9", "OLS exact-match search hit (label or synonym)"),
        ("ols_fuzzy", "<0.9", "OLS multi-match disambiguated by row context"),
        ("project_local", "varies", "private IRI in apecx_local namespace (no external mapping)"),
        (
            "unresolved",
            "0.0",
            "no mapping; row stays with canonical_iri=None — surfaced explicitly at query time",
        ),
    ]

    # Header row
    H_Y = 1.55
    ax.text(0.55, H_Y, "status", fontsize=9, fontweight="bold", color="#666")
    ax.text(2.4, H_Y, "confidence", fontsize=9, fontweight="bold", color="#666")
    ax.text(4.0, H_Y, "meaning", fontsize=9, fontweight="bold", color="#666")

    for i, (status, conf, desc) in enumerate(statuses):
        y = 1.30 - i * 0.26
        ax.text(
            0.55, y, status, fontsize=10, fontweight="bold", family="monospace", color="#306030"
        )
        ax.text(2.4, y, conf, fontsize=10, family="monospace", color="#666")
        ax.text(4.0, y, desc, fontsize=9.5, color="#333")

    ax.set_xlim(0, 17.0)
    ax.set_ylim(0, 11.0)
    ax.axis("off")
    fig.savefig(OUT_DIR / "10_backend_harmonization.png")
    plt.close(fig)


# ---------------------------------------------------------------------
# Figure 11 — User-facing query workflow (online, per-query, real-time)
# ---------------------------------------------------------------------


def fig_user_facing_workflow() -> None:
    """The ONLINE per-query path. Reads artifacts that the backend
    pipeline produced; never writes to them. Three vertically-stacked
    bands with generous gutters: (1) MCP entry, (2) synthesize_query
    expanded, (3) artifacts consumed."""
    fig, ax = plt.subplots(figsize=(17, 11))
    palette = sns.color_palette("Set2", n_colors=8)

    # Heading band
    _box(ax, 0.0, 10.4, 17.0, 0.6, "", palette[5], 11)
    ax.text(
        8.5,
        10.7,
        "USER-FACING WORKFLOW  ·  online  ·  per-query  ·  ~70s wall-clock on Ollama",
        ha="center",
        fontsize=14,
        fontweight="bold",
        color="white",
    )

    # ============================================================
    # Band 1 — MCP entry (top)
    # ============================================================
    ax.text(0.3, 9.95, "BAND 1  ·  MCP ENTRY", fontsize=11, fontweight="bold", color="#666")

    _box(ax, 0.3, 9.0, 3.0, 0.75, "scientist\nfree-text query", palette[0], 10.5, "bold")
    _box(
        ax, 4.0, 9.0, 4.5, 0.75, "MCP client\nClaude Desktop / IDE / CLI (stdio)", palette[1], 10.5
    )
    _box(
        ax,
        9.2,
        9.0,
        7.4,
        0.75,
        "FastMCP server  —  apecx-mcp-integration  (23 tools)",
        palette[2],
        11,
        "bold",
    )

    _arrow(ax, 3.3, 9.38, 4.0, 9.38, lw=1.5)
    _arrow(ax, 8.5, 9.38, 9.2, 9.38, lw=1.5)

    # Tool dispatch
    _box(ax, 0.3, 7.9, 4.0, 0.7, "synthesize_query\n(this band's focus)", palette[3], 10, "bold")
    _box(ax, 4.5, 7.9, 4.0, 0.7, "query_globus_search\nfree-text Globus tool", palette[6], 9.5)
    _box(
        ax,
        8.7,
        7.9,
        4.0,
        0.7,
        "query_pathogens / vaccines / ...\nstructured DB lookup (7)",
        palette[6],
        9.5,
    )
    _box(ax, 12.9, 7.9, 3.7, 0.7, "resolve_canonical_entity\nfast-path resolution", palette[6], 9.5)

    _arrow(ax, 12.9, 9.0, 2.3, 8.6, color="#666", lw=0.9)
    _arrow(ax, 12.9, 9.0, 6.5, 8.6, color="#666", lw=0.9)
    _arrow(ax, 12.9, 9.0, 10.7, 8.6, color="#666", lw=0.9)
    _arrow(ax, 12.9, 9.0, 14.75, 8.6, color="#666", lw=0.9)

    # ============================================================
    # Band 2 — synthesize_query expanded
    # ============================================================
    ax.text(
        0.3,
        7.4,
        "BAND 2  ·  SYNTHESIZE_QUERY EXPANDED",
        fontsize=11,
        fontweight="bold",
        color="#a04040",
    )

    # Assembly container
    _box(ax, 0.3, 3.4, 9.0, 3.7, "", palette[1], 9)
    ax.text(4.8, 6.85, "SynthesisContextAssemblyStep", ha="center", fontsize=12, fontweight="bold")

    # gather hub
    _box(ax, 0.5, 5.4, 2.3, 0.85, "asyncio.gather\nreturn_exceptions=True", palette[2], 10, "bold")

    # 4 retrieval branches  (clearly OUTSIDE the gather box, no overlap)
    branches = [
        (6.05, "FAISS RAG search\nin-memory  ·  ~5 ms", "rag_chunks"),
        (
            5.10,
            "VIOLIN / BV-BRC pandas\noffline CSV+TSV  ·  ~50 ms",
            "violin_mappings + bvbrc_genomes",
        ),
        (4.15, "PubMed eSearch + eFetch\nnetwork  ·  1–3 s", "publications"),
        (3.20, "Globus Search\nnetwork  ·  ~500 ms", "globus_results"),
    ]
    for y, lbl, _ in branches:
        _box(ax, 3.4, y, 5.7, 0.85, lbl, palette[4], 9.5)

    # Bundle (below assembly box but inside band 2)
    _box(
        ax,
        0.3,
        2.55,
        9.0,
        0.7,
        "synthesis_bundle_output  ·  {query, rag_chunks, bvbrc_genomes, violin_mappings, publications, globus_results}",
        palette[5],
        9,
        "bold",
    )

    # Synthesis container
    _box(ax, 9.7, 3.4, 7.0, 3.7, "", palette[3], 9)
    ax.text(13.2, 6.85, "RagSynthesisStep", ha="center", fontsize=12, fontweight="bold")
    _box(
        ax,
        9.9,
        5.7,
        6.6,
        0.85,
        "synthesize_response\nsingle LLM round-trip  ·  30–60 s",
        palette[2],
        10,
        "bold",
    )
    _box(
        ax,
        9.9,
        4.55,
        6.6,
        0.85,
        "validation gates\nsize · grounded · empty-retrieval",
        palette[2],
        9.5,
    )
    _box(
        ax,
        9.9,
        3.55,
        6.6,
        0.85,
        "synthesis_output\n{synthesis: <markdown>}",
        palette[5],
        10,
        "bold",
    )

    # Synthesis → markdown answer (in band 2 too)
    _box(
        ax,
        9.7,
        2.55,
        7.0,
        0.7,
        "Markdown answer with inline citations  ·  returned to MCP client",
        palette[0],
        10.5,
        "bold",
    )

    # Arrows in band 2
    _arrow(ax, 2.3, 7.9, 1.65, 6.25, color="#a04040", lw=1.5)
    for y, _, _ in branches:
        _arrow(ax, 2.8, 5.85, 3.4, y + 0.42, color="#666", lw=0.9)
    for y, _, _ in branches:
        _arrow(ax, 6.3, y, 6.3, 3.25, color="#888", lw=0.7)
    _arrow(ax, 9.3, 2.9, 9.7, 4.05, color="#306060", lw=1.8)
    _arrow(ax, 13.2, 5.7, 13.2, 5.4, lw=1.4)
    _arrow(ax, 13.2, 4.55, 13.2, 4.4, lw=1.4)
    _arrow(ax, 13.2, 3.55, 13.2, 3.25, lw=1.4)

    # ============================================================
    # Band 3 — artifacts consumed (read-only)
    # ============================================================
    ax.text(
        0.3,
        1.95,
        "BAND 3  ·  ARTIFACTS CONSUMED  (read-only)",
        fontsize=11,
        fontweight="bold",
        color="#666",
    )

    arts = [
        (0.3, "FAISS index\nbuilt by build_domain_rag_index.py", palette[7]),
        (4.5, "VIOLIN CSVs / BV-BRC TSV\nvendored, refreshed by apecx-setup", palette[7]),
        (8.7, "Globus Search index\nbuilt by harvester pipeline", palette[7]),
        (12.9, "synonym_dictionary SQLite\nbuilt by dictionary_build_workflow", palette[7]),
    ]
    for x, lbl, c in arts:
        _box(ax, x, 0.7, 4.0, 1.0, lbl, c, 10)

    # Band 2 → Band 3 arrows (which artifact serves which branch)
    _arrow(ax, 6.3, 3.2, 2.3, 1.7, color="#888", lw=0.8)
    _arrow(ax, 6.3, 3.2, 6.5, 1.7, color="#888", lw=0.8)
    _arrow(ax, 6.3, 3.2, 10.7, 1.7, color="#888", lw=0.8)
    _arrow(ax, 14.75, 7.9, 14.9, 1.7, color="#888", lw=0.8)

    # Footer: timing summary
    ax.text(
        0.3,
        0.25,
        "Total wall-clock budget: ~5–10s retrieval (mostly PubMed) + ~30–60s LLM = ~70s end-to-end on local Ollama.",
        fontsize=10,
        color="#444",
        style="italic",
    )

    ax.set_xlim(0, 17.0)
    ax.set_ylim(0, 11.0)
    ax.axis("off")
    fig.savefig(OUT_DIR / "11_user_facing_workflow.png")
    plt.close(fig)


# ---------------------------------------------------------------------
# Figure 12 — Accuracy thresholds (per entity class, slice vs full corpus)
#
# Source of truth for the numbers:
#   tests/integration/test_synonym_accuracy.py — the enforced floors
#   src/apecx_integration/synonym_dictionary/metrics.py — AccuracyMetrics
#
# These are LOWER BOUNDS, not historical run numbers. The test suite
# fails CI if a build drops below any of them. We show them as ranges
# (floor → 1.0) so an engineer reading the slide doesn't mistake the
# threshold for the observed value.
# ---------------------------------------------------------------------


def fig_accuracy_thresholds() -> None:
    # Real floors from test_synonym_accuracy.py (verified 2026-05-05).
    # n_rows is the per-class slice size (slice baseline) / full-corpus row count.
    classes = [
        {
            "name": "Pathogen",
            "n_full": 218,
            "metrics": [
                ("Recall", 0.95, 0.90),
                ("Precision", 0.95, 0.95),
                ("F1", 0.95, 0.92),
            ],
        },
        {
            "name": "Vaccine",
            "n_full": 3507,
            "metrics": [
                ("Recall", 0.80, 0.75),
                ("Precision", 0.80, 0.85),
            ],
        },
        {
            "name": "Gene",
            "n_full": 4063,
            "metrics": [
                ("Recall", 0.70, 0.65),
                ("Precision", 0.95, 0.95),
            ],
        },
    ]
    palette = sns.color_palette("muted", n_colors=4)
    color_slice = palette[1]
    color_full = palette[3]

    fig, axes = plt.subplots(1, 3, figsize=(15, 5.2), sharex=True)
    bar_h = 0.36
    for ax, cls in zip(axes, classes, strict=True):
        labels = [m[0] for m in cls["metrics"]]
        slice_vals = [m[1] for m in cls["metrics"]]
        full_vals = [m[2] for m in cls["metrics"]]
        y = np.arange(len(labels))

        ax.barh(
            y + bar_h / 2,
            slice_vals,
            height=bar_h,
            color=color_slice,
            alpha=ALPHA,
            edgecolor="#333",
            linewidth=0.5,
            label="slice (60 rows) baseline floor",
        )
        ax.barh(
            y - bar_h / 2,
            full_vals,
            height=bar_h,
            color=color_full,
            alpha=ALPHA,
            edgecolor="#333",
            linewidth=0.5,
            label=f"full corpus floor ({cls['n_full']:,} rows, CI-enforced)",
        )
        for yi, sv, fv in zip(y, slice_vals, full_vals, strict=True):
            ax.text(
                sv + 0.012, yi + bar_h / 2, f"{sv:.2f}", va="center", fontsize=9, fontweight="bold"
            )
            ax.text(
                fv + 0.012, yi - bar_h / 2, f"{fv:.2f}", va="center", fontsize=9, fontweight="bold"
            )

        ax.axvline(1.0, color="#888", linestyle=":", linewidth=0.8, alpha=0.6)
        ax.set_yticks(y)
        ax.set_yticklabels(labels, fontsize=11)
        ax.set_xlim(0, 1.18)
        ax.invert_yaxis()
        ax.set_title(f"{cls['name']}  ·  n = {cls['n_full']:,}", fontsize=12, fontweight="bold")
        ax.set_xlabel("Score (0–1)", fontsize=10)
        ax.legend(loc="lower right", fontsize=8.5, frameon=False)
        sns.despine(ax=ax, left=False)

    fig.suptitle(
        "Synonym-resolution accuracy — CI-enforced lower bounds (1.0 = perfect)",
        fontsize=13,
        fontweight="bold",
        y=1.02,
    )
    fig.text(
        0.5,
        -0.06,
        "Bars are LOWER BOUNDS in tests/integration/test_synonym_accuracy.py — a build that fails any floor fails CI.\n"
        "Vaccine + Gene F1 floors are not separately enforced (recall + precision floors suffice). "
        "Disease has no recall floor (search-only).",
        ha="center",
        fontsize=8.5,
        color="#444",
        style="italic",
    )
    fig.tight_layout()
    fig.savefig(OUT_DIR / "12_accuracy_thresholds.png", bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------
# Figure 13 — Harmonization statistics
#
# Real, verified numbers:
#   - Source row counts (wc -l on the actual CSVs/TSVs in data/, 2026-05-05)
#   - Ontology coverage (synonym_dictionary/enums.py)
#   - Resolution status taxonomy (synonym_dictionary/enums.py)
# ---------------------------------------------------------------------


def fig_harmonization_stats() -> None:
    fig, axes = plt.subplots(1, 2, figsize=(15, 6))
    palette = sns.color_palette("muted", n_colors=8)

    # ---- Left: source corpus row counts (real, verified 2026-05-05) ----
    ax1 = axes[0]
    sources = [
        ("VIOLIN Pathogens", 218),
        ("VIOLIN Vaccines", 3_507),
        ("VIOLIN Genes", 4_063),
        ("BV-BRC genomes", 5_450),
    ]
    labels = [s[0] for s in sources]
    counts = [s[1] for s in sources]
    colors = [palette[0], palette[1], palette[2], palette[3]]

    bars = ax1.barh(
        np.arange(len(labels)),
        counts,
        color=colors,
        alpha=ALPHA,
        edgecolor="#333",
        linewidth=0.6,
    )
    for bar, n in zip(bars, counts, strict=True):
        ax1.text(
            bar.get_width() + max(counts) * 0.01,
            bar.get_y() + bar.get_height() / 2,
            f"{n:,}",
            va="center",
            fontsize=10,
            fontweight="bold",
        )

    ax1.set_yticks(np.arange(len(labels)))
    ax1.set_yticklabels(labels, fontsize=10)
    ax1.invert_yaxis()
    ax1.set_xlabel("Source rows", fontsize=11)
    ax1.set_title(
        "Source corpus  ·  13,238 total rows  ·  verified 2026-05-05", fontsize=11.5, pad=10
    )
    ax1.set_xlim(0, max(counts) * 1.15)
    sns.despine(ax=ax1, left=True)

    # ---- Right: resolution status confidence buckets ----
    ax2 = axes[1]
    statuses = [
        ("id_anchored", 1.0),
        ("ols_exact", 0.9),
        ("ols_fuzzy", 0.7),
        ("project_local", 0.5),
        ("unresolved", 0.0),
    ]
    s_labels = [s[0] for s in statuses]
    s_conf = [s[1] for s in statuses]
    s_colors = [palette[4], palette[5], palette[6], palette[7], palette[3]]

    bars2 = ax2.barh(
        np.arange(len(s_labels)),
        s_conf,
        color=s_colors,
        alpha=ALPHA,
        edgecolor="#333",
        linewidth=0.6,
    )
    for bar, c in zip(bars2, s_conf, strict=True):
        ax2.text(
            bar.get_width() + 0.01,
            bar.get_y() + bar.get_height() / 2,
            f"{c:.1f}" if c > 0 else "0.0",
            va="center",
            fontsize=10,
            fontweight="bold",
        )

    ax2.set_yticks(np.arange(len(s_labels)))
    ax2.set_yticklabels(s_labels, fontsize=10, family="monospace")
    ax2.invert_yaxis()
    ax2.set_xlabel("Confidence value (returned to caller)", fontsize=11)
    ax2.set_title(
        "Resolution status  ·  written by build.py, surfaced at query time", fontsize=11.5, pad=10
    )
    ax2.set_xlim(0, 1.15)
    sns.despine(ax=ax2, left=True)

    fig.tight_layout()
    fig.savefig(OUT_DIR / "13_harmonization_stats.png")
    plt.close(fig)


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------


def main() -> None:
    fig_three_tier_topology()
    fig_synthesis_pipeline()
    fig_invocation_paths()
    fig_resolution_decision_tree()
    fig_test_surface()
    fig_ontologies_coverage()
    fig_mcp_tool_distribution()
    fig_trigger_cascade_timeline()
    fig_session_bug_count()
    fig_backend_harmonization()
    fig_user_facing_workflow()
    fig_accuracy_thresholds()
    fig_harmonization_stats()
    print("Figures written to", OUT_DIR)
    for p in sorted(OUT_DIR.glob("*.png")):
        print(f"  {p.name}: {p.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
