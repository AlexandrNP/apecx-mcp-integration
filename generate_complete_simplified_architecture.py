#!/usr/bin/env python3
"""
Generate complete simplified viral epitope analysis architecture diagram.
Includes synonym substitution component and complete workflow.
"""

import matplotlib.pyplot as plt
import seaborn as sns
from matplotlib.patches import FancyBboxPatch

# Set up the style
plt.style.use("seaborn-v0_8-whitegrid")
sns.set_palette("husl")

# Define consistent colors
COLORS = {
    "input": "#3498db",  # Blue
    "processing": "#f39c12",  # Orange
    "llm": "#e74c3c",  # Red
    "data_source": "#2ecc71",  # Green
    "output": "#9b59b6",  # Purple
    "user": "#34495e",  # Dark blue-gray
    "synonym": "#e67e22",  # Darker orange
}


def create_complete_simplified_architecture():
    """Create complete simplified architecture with synonym substitution."""

    fig, ax = plt.subplots(1, 1, figsize=(16, 14))
    ax.set_xlim(0, 14)
    ax.set_ylim(0, 13)
    ax.axis("off")

    # Title
    ax.text(
        7,
        12.5,
        "Viral Epitope Analysis Architecture",
        fontsize=22,
        fontweight="bold",
        ha="center",
        va="center",
    )

    # User query input
    user_box = FancyBboxPatch(
        (5.5, 11.2),
        3,
        0.6,
        boxstyle="round,pad=0.1",
        facecolor=COLORS["user"],
        alpha=0.7,
        edgecolor="black",
        linewidth=2,
    )
    ax.add_patch(user_box)
    ax.text(
        7,
        11.5,
        "Natural Language Query",
        fontsize=12,
        fontweight="bold",
        ha="center",
        va="center",
        color="white",
    )

    # Synonym substitution step
    synonym_box = FancyBboxPatch(
        (5, 10),
        4,
        1,
        boxstyle="round,pad=0.1",
        facecolor=COLORS["synonym"],
        alpha=0.7,
        edgecolor="black",
        linewidth=2,
    )
    ax.add_patch(synonym_box)
    ax.text(
        7,
        10.6,
        "Synonym Dictionary",
        fontsize=13,
        fontweight="bold",
        ha="center",
        va="center",
        color="white",
    )
    ax.text(
        7,
        10.3,
        "Entity Normalization & Term Mapping",
        fontsize=10,
        ha="center",
        va="center",
        color="white",
        style="italic",
    )

    # Synonym dictionary database (side component)
    dict_box = FancyBboxPatch(
        (10.5, 9.8),
        2.5,
        1.4,
        boxstyle="round,pad=0.08",
        facecolor=COLORS["data_source"],
        alpha=0.7,
        edgecolor="black",
        linewidth=1.5,
    )
    ax.add_patch(dict_box)
    ax.text(
        11.75,
        10.7,
        "Synonym\nDatabase",
        fontsize=11,
        fontweight="bold",
        ha="center",
        va="center",
        color="white",
    )
    ax.text(
        11.75,
        10.2,
        "Taxdump\nVIOLIN Terms\nCanonical Names",
        fontsize=9,
        ha="center",
        va="center",
        color="white",
        style="italic",
    )

    # Core workflow steps - positioned below synonym processing
    steps = [
        # (x, y, width, height, label, description, color)
        (
            0.5,
            7.5,
            3.5,
            1.5,
            "Viral Query\nClassifier",
            "Virus Family Detection\nProtein Identification\nResearch Type Analysis",
            COLORS["processing"],
        ),
        (
            4.75,
            7.5,
            3.5,
            1.5,
            "Multi-Source\nData Assembly",
            "Unlimited Data Retrieval\nQuality-Based Filtering\nCross-Source Integration",
            COLORS["data_source"],
        ),
        (
            9,
            7.5,
            3.5,
            1.5,
            "LLM Synthesis\nEngine",
            "Mistral-Nemo Processing\nGrounded Citation Generation\nScientific Analysis",
            COLORS["llm"],
        ),
    ]

    # Draw workflow steps
    for x, y, w, h, label, desc, color in steps:
        # Main step box
        box = FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0.1",
            facecolor=color,
            alpha=0.7,
            edgecolor="black",
            linewidth=2,
        )
        ax.add_patch(box)

        # Step label
        ax.text(
            x + w / 2,
            y + h * 0.75,
            label,
            fontsize=13,
            fontweight="bold",
            ha="center",
            va="center",
            color="white",
        )

        # Description
        ax.text(
            x + w / 2,
            y + h * 0.35,
            desc,
            fontsize=9,
            ha="center",
            va="center",
            color="white",
            style="italic",
        )

    # Data sources positioned below assembly step
    data_sources = [
        # (x, y, width, height, label, details)
        (0.2, 5, 2.2, 1.3, "VIOLIN\nDatabase", "Vaccine & Epitope\nMappings\n2,545+ results"),
        (2.6, 5, 2.2, 1.3, "BV-BRC\nGenomes", "Bacterial & Viral\nGenome Data\nStructural Info"),
        (5, 5, 2.2, 1.3, "RAG Index\nFAISS", "Scientific Literature\nVector Search\n20 top chunks"),
        (
            7.4,
            5,
            2.2,
            1.3,
            "Globus\nSearch",
            "Research Data Portal\nStructural Databases\n100+ structures",
        ),
        (
            9.8,
            5,
            2.2,
            1.3,
            "PubMed\nAPI",
            "Biomedical Literature\nPeer-Reviewed Papers\nCitation Sources",
        ),
    ]

    # Draw data sources
    for x, y, w, h, label, details in data_sources:
        # Data source box
        box = FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0.08",
            facecolor=COLORS["data_source"],
            alpha=0.7,
            edgecolor="black",
            linewidth=1.5,
        )
        ax.add_patch(box)

        # Label
        ax.text(
            x + w / 2,
            y + h * 0.7,
            label,
            fontsize=10,
            fontweight="bold",
            ha="center",
            va="center",
            color="white",
        )

        # Details
        ax.text(
            x + w / 2,
            y + h * 0.3,
            details,
            fontsize=8,
            ha="center",
            va="center",
            color="white",
            style="italic",
        )

    # LLM service box
    llm_box = FancyBboxPatch(
        (5, 3),
        4,
        1.2,
        boxstyle="round,pad=0.1",
        facecolor=COLORS["llm"],
        alpha=0.7,
        edgecolor="black",
        linewidth=2,
    )
    ax.add_patch(llm_box)
    ax.text(
        7,
        3.8,
        "Mistral-Nemo LLM Service",
        fontsize=12,
        fontweight="bold",
        ha="center",
        va="center",
        color="white",
    )
    ax.text(
        7,
        3.4,
        "Ollama • Local Deployment • Chat Completions API",
        fontsize=10,
        ha="center",
        va="center",
        color="white",
        style="italic",
    )

    # Output analysis box
    output_box = FancyBboxPatch(
        (3.5, 1.2),
        7,
        1.3,
        boxstyle="round,pad=0.1",
        facecolor=COLORS["output"],
        alpha=0.7,
        edgecolor="black",
        linewidth=2,
    )
    ax.add_patch(output_box)
    ax.text(
        7,
        2.1,
        "Comprehensive Scientific Analysis",
        fontsize=14,
        fontweight="bold",
        ha="center",
        va="center",
        color="white",
    )
    ax.text(
        7,
        1.6,
        "Virus Classification • Epitope Mapping • Research Insights • Grounded Citations",
        fontsize=10,
        ha="center",
        va="center",
        color="white",
        style="italic",
    )

    # Connection arrows
    arrow_props = {"arrowstyle": "->", "lw": 2.5, "color": "#2c3e50"}

    # User query to synonym substitution
    ax.annotate("", xy=(7, 11.2), xytext=(7, 11.8), arrowprops=arrow_props)

    # Synonym dictionary to synonym substitution
    ax.annotate(
        "",
        xy=(10.5, 10.5),
        xytext=(9, 10.5),
        arrowprops={"arrowstyle": "->", "lw": 1.5, "color": "orange"},
    )

    # Synonym substitution to classifier
    ax.annotate("", xy=(2.25, 9), xytext=(6.5, 10), arrowprops=arrow_props)

    # Classifier to assembly
    ax.annotate("", xy=(4.75, 8.25), xytext=(4, 8.25), arrowprops=arrow_props)

    # Assembly to synthesis
    ax.annotate("", xy=(9, 8.25), xytext=(8.25, 8.25), arrowprops=arrow_props)

    # Assembly to data sources (multiple arrows)
    assembly_center_x, assembly_center_y = 6.5, 7.5
    for x, y, w, h, _, _ in data_sources:
        source_center_x = x + w / 2
        source_center_y = y + h
        ax.annotate(
            "",
            xy=(source_center_x, source_center_y),
            xytext=(assembly_center_x, assembly_center_y),
            arrowprops={"arrowstyle": "->", "lw": 1.5, "color": "green", "alpha": 0.8},
        )

    # Synthesis to LLM (bidirectional)
    ax.annotate(
        "",
        xy=(7, 4.2),
        xytext=(10.75, 7.5),
        arrowprops={"arrowstyle": "->", "lw": 2, "color": "red", "alpha": 0.8},
    )
    ax.annotate(
        "",
        xy=(10, 7.8),
        xytext=(7.5, 4),
        arrowprops={"arrowstyle": "->", "lw": 1.5, "color": "red", "alpha": 0.6},
    )

    # Synthesis to output
    ax.annotate(
        "",
        xy=(7, 2.5),
        xytext=(10.75, 7.5),
        arrowprops={"arrowstyle": "->", "lw": 2.5, "color": "purple", "alpha": 0.8},
    )

    # Add workflow stages labels
    stage_labels = [
        (1.5, 11.5, "1. Normalization"),
        (2.25, 6.8, "2. Classification"),
        (6.5, 6.8, "3. Assembly"),
        (10.75, 6.8, "4. Synthesis"),
    ]

    for x, y, label in stage_labels:
        ax.text(
            x,
            y,
            label,
            fontsize=11,
            fontweight="bold",
            ha="center",
            va="center",
            bbox={"boxstyle": "round,pad=0.3", "facecolor": "white", "alpha": 0.8},
        )

    # Add performance indicators
    perf_box = FancyBboxPatch(
        (0.2, 0.3),
        6,
        0.6,
        boxstyle="round,pad=0.05",
        facecolor="lightgray",
        alpha=0.3,
        edgecolor="gray",
        linewidth=1,
    )
    ax.add_patch(perf_box)
    ax.text(
        3.2,
        0.6,
        "Performance: 5,200+ data points • 1.000 confidence • <2min processing",
        fontsize=10,
        fontweight="bold",
        ha="center",
        va="center",
        color="#2c3e50",
    )

    # Add capabilities indicator
    cap_box = FancyBboxPatch(
        (7.8, 0.3),
        6,
        0.6,
        boxstyle="round,pad=0.05",
        facecolor="lightgray",
        alpha=0.3,
        edgecolor="gray",
        linewidth=1,
    )
    ax.add_patch(cap_box)
    ax.text(
        10.8,
        0.6,
        "Multi-Virus: COVID-19 • Influenza • EEEV • Zika • HIV • Any virus family",
        fontsize=10,
        fontweight="bold",
        ha="center",
        va="center",
        color="#2c3e50",
    )

    plt.tight_layout()
    plt.savefig("viral_workflow_complete_simplified.png", dpi=300, bbox_inches="tight")
    print("✅ Generated: viral_workflow_complete_simplified.png")
    plt.close()


def main():
    """Generate complete simplified architecture with synonym substitution."""
    print("🎨 Generating complete viral epitope analysis architecture...")
    print("📊 Including synonym substitution and normalization components")

    create_complete_simplified_architecture()

    print("\n🎉 Complete simplified architecture generated!")
    print("📁 Generated file: viral_workflow_complete_simplified.png")
    print("\n🔍 Features:")
    print("   ✅ Synonym dictionary and substitution included")
    print("   ✅ Entity normalization process shown")
    print("   ✅ Complete 4-stage workflow")
    print("   ✅ LLM integration clearly marked")
    print("   ✅ All data sources with metrics")
    print("   ✅ Alpha=0.7 transparency maintained")


if __name__ == "__main__":
    main()
