#!/usr/bin/env python3
"""
Generate simplified viral epitope analysis architecture diagram.
Focuses on core workflow and data sources, excluding framework-specific components.
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
}


def create_simplified_architecture():
    """Create simplified architecture diagram without framework components."""

    fig, ax = plt.subplots(1, 1, figsize=(16, 12))
    ax.set_xlim(0, 14)
    ax.set_ylim(0, 12)
    ax.axis("off")

    # Title
    ax.text(
        7,
        11.5,
        "Viral Epitope Analysis Architecture",
        fontsize=22,
        fontweight="bold",
        ha="center",
        va="center",
    )

    # User query input
    user_box = FancyBboxPatch(
        (5.5, 10),
        3,
        0.8,
        boxstyle="round,pad=0.1",
        facecolor=COLORS["user"],
        alpha=0.7,
        edgecolor="black",
        linewidth=2,
    )
    ax.add_patch(user_box)
    ax.text(
        7,
        10.4,
        "Natural Language Query",
        fontsize=12,
        fontweight="bold",
        ha="center",
        va="center",
        color="white",
    )

    # Core workflow steps - positioned horizontally
    steps = [
        # (x, y, width, height, label, description, color)
        (
            1,
            8,
            3.5,
            1.5,
            "Viral Query\nClassifier",
            "Virus Family Detection\nProtein Identification\nResearch Type Analysis",
            COLORS["processing"],
        ),
        (
            5.25,
            8,
            3.5,
            1.5,
            "Multi-Source\nData Assembly",
            "Unlimited Data Retrieval\nQuality-Based Filtering\nCross-Source Integration",
            COLORS["data_source"],
        ),
        (
            9.5,
            8,
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
        # (x, y, width, height, label, details, icon)
        (
            0.5,
            5.5,
            2.2,
            1.3,
            "VIOLIN\nDatabase",
            "Vaccine & Epitope\nMappings\n2,545+ results",
            "🧬",
        ),
        (
            3,
            5.5,
            2.2,
            1.3,
            "BV-BRC\nGenomes",
            "Bacterial & Viral\nGenome Data\nStructural Info",
            "🦠",
        ),
        (
            5.5,
            5.5,
            2.2,
            1.3,
            "RAG Index\nFAISS",
            "Scientific Literature\nVector Search\n20 top chunks",
            "📚",
        ),
        (
            8,
            5.5,
            2.2,
            1.3,
            "Globus\nSearch",
            "Research Data Portal\nStructural Databases\n100+ structures",
            "🔬",
        ),
        (
            10.5,
            5.5,
            2.2,
            1.3,
            "PubMed\nAPI",
            "Biomedical Literature\nPeer-Reviewed Papers\nCitation Sources",
            "📄",
        ),
    ]

    # Draw data sources
    for x, y, w, h, label, details, icon in data_sources:
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

        # Icon (using text)
        ax.text(x + w / 2, y + h * 0.85, icon, fontsize=16, ha="center", va="center")

        # Label
        ax.text(
            x + w / 2,
            y + h * 0.6,
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
            y + h * 0.25,
            details,
            fontsize=8,
            ha="center",
            va="center",
            color="white",
            style="italic",
        )

    # LLM service box
    llm_box = FancyBboxPatch(
        (5.5, 3.5),
        3,
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
        4.3,
        "🤖 Mistral-Nemo LLM",
        fontsize=12,
        fontweight="bold",
        ha="center",
        va="center",
        color="white",
    )
    ax.text(
        7,
        3.9,
        "Ollama Service • Local Deployment",
        fontsize=10,
        ha="center",
        va="center",
        color="white",
        style="italic",
    )

    # Output analysis box
    output_box = FancyBboxPatch(
        (4, 1.5),
        6,
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
        2.4,
        "📋 Comprehensive Scientific Analysis",
        fontsize=14,
        fontweight="bold",
        ha="center",
        va="center",
        color="white",
    )
    ax.text(
        7,
        1.9,
        "Virus Classification • Epitope Mapping • Research Insights • Grounded Citations",
        fontsize=10,
        ha="center",
        va="center",
        color="white",
        style="italic",
    )

    # Connection arrows
    arrow_props = {"arrowstyle": "->", "lw": 2.5, "color": "#2c3e50"}

    # User query to classifier
    ax.annotate("", xy=(2.75, 9.5), xytext=(6.5, 10), arrowprops=arrow_props)

    # Classifier to assembly
    ax.annotate("", xy=(5.25, 8.75), xytext=(4.5, 8.75), arrowprops=arrow_props)

    # Assembly to synthesis
    ax.annotate("", xy=(9.5, 8.75), xytext=(8.75, 8.75), arrowprops=arrow_props)

    # Assembly to data sources (multiple arrows)
    assembly_center_x, assembly_center_y = 7, 8
    for x, y, w, h, _, _, _ in data_sources:
        source_center_x = x + w / 2
        source_center_y = y + h
        ax.annotate(
            "",
            xy=(source_center_x, source_center_y),
            xytext=(assembly_center_x, assembly_center_y),
            arrowprops={"arrowstyle": "->", "lw": 1.5, "color": "green", "alpha": 0.8},
        )

    # Synthesis to LLM
    ax.annotate(
        "",
        xy=(7, 4.7),
        xytext=(11.25, 8),
        arrowprops={"arrowstyle": "->", "lw": 2, "color": "red", "alpha": 0.8},
    )

    # LLM back to synthesis (bidirectional)
    ax.annotate(
        "",
        xy=(10.5, 8.3),
        xytext=(7.5, 4.5),
        arrowprops={"arrowstyle": "->", "lw": 1.5, "color": "red", "alpha": 0.6},
    )

    # Synthesis to output
    ax.annotate(
        "",
        xy=(7, 2.8),
        xytext=(11.25, 8),
        arrowprops={"arrowstyle": "->", "lw": 2.5, "color": "purple", "alpha": 0.8},
    )

    # Add performance indicators
    perf_box = FancyBboxPatch(
        (0.5, 0.5),
        4,
        0.8,
        boxstyle="round,pad=0.05",
        facecolor="lightgray",
        alpha=0.3,
        edgecolor="gray",
        linewidth=1,
    )
    ax.add_patch(perf_box)
    ax.text(
        2.5,
        0.9,
        "⚡ Performance: 5,200+ data points • 1.000 confidence • <2min processing",
        fontsize=10,
        fontweight="bold",
        ha="center",
        va="center",
        color="#2c3e50",
    )

    # Add capabilities indicator
    cap_box = FancyBboxPatch(
        (9.5, 0.5),
        4,
        0.8,
        boxstyle="round,pad=0.05",
        facecolor="lightgray",
        alpha=0.3,
        edgecolor="gray",
        linewidth=1,
    )
    ax.add_patch(cap_box)
    ax.text(
        11.5,
        0.9,
        "🦠 Supports: COVID-19 • Influenza • EEEV • Zika • HIV • Any virus",
        fontsize=10,
        fontweight="bold",
        ha="center",
        va="center",
        color="#2c3e50",
    )

    plt.tight_layout()
    plt.savefig("viral_workflow_simplified_architecture.png", dpi=300, bbox_inches="tight")
    print("✅ Generated: viral_workflow_simplified_architecture.png")
    plt.close()


def main():
    """Generate simplified architecture visualization."""
    print("🎨 Generating simplified viral epitope analysis architecture...")
    print("📊 Excluding framework-specific components (DataUnits, Triggers, etc.)")

    create_simplified_architecture()

    print("\n🎉 Simplified architecture visualization generated!")
    print("📁 Generated file: viral_workflow_simplified_architecture.png")
    print("\n🔍 Features:")
    print("   ✅ Core workflow components only")
    print("   ✅ LLM integration clearly marked")
    print("   ✅ Data sources with performance metrics")
    print("   ✅ Clean, business-focused view")
    print("   ✅ Alpha=0.7 transparency maintained")


if __name__ == "__main__":
    main()
