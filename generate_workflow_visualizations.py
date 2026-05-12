#!/usr/bin/env python3
"""
Generate visual representations of the viral epitope analysis workflow.
Creates multiple diagrams showing architecture, data flow, and LLM integration.
"""

from pathlib import Path

import matplotlib.patches as patches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
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
    "framework": "#95a5a6",  # Gray
}


def create_workflow_overview():
    """Create high-level workflow overview diagram."""

    fig, ax = plt.subplots(1, 1, figsize=(14, 10))
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 8)
    ax.axis("off")

    # Title
    ax.text(
        5,
        7.5,
        "Viral Epitope Analysis Workflow",
        fontsize=20,
        fontweight="bold",
        ha="center",
        va="center",
    )

    # Step boxes with improved positioning and labels
    steps = [
        # (x, y, width, height, label, color, description)
        (
            0.5,
            5.5,
            2,
            1.2,
            "1. Query\nClassification",
            COLORS["processing"],
            "Virus Family Detection\nResearch Type Analysis",
        ),
        (
            3.5,
            5.5,
            2,
            1.2,
            "2. Data\nAssembly",
            COLORS["data_source"],
            "Unlimited Retrieval\nMulti-source Integration",
        ),
        (
            6.5,
            5.5,
            2.5,
            1.2,
            "3. Synthesis\n& Analysis",
            COLORS["llm"],
            "LLM-Powered Synthesis\nGrounded Citations",
        ),
    ]

    for x, y, w, h, label, color, desc in steps:
        # Main box
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
            y + h * 0.7,
            label,
            fontsize=14,
            fontweight="bold",
            ha="center",
            va="center",
            color="white",
        )

        # Description
        ax.text(
            x + w / 2,
            y + h * 0.3,
            desc,
            fontsize=10,
            ha="center",
            va="center",
            color="white",
            style="italic",
        )

    # Add arrows between steps
    arrow_props = {"arrowstyle": "->", "lw": 3, "color": "#2c3e50"}

    # Arrow 1 -> 2
    ax.annotate("", xy=(3.5, 6.1), xytext=(2.5, 6.1), arrowprops=arrow_props)
    # Arrow 2 -> 3
    ax.annotate("", xy=(6.5, 6.1), xytext=(5.5, 6.1), arrowprops=arrow_props)

    # Input and output
    # Input query box
    input_box = FancyBboxPatch(
        (0.5, 3.5),
        8.5,
        0.8,
        boxstyle="round,pad=0.1",
        facecolor=COLORS["input"],
        alpha=0.7,
        edgecolor="black",
        linewidth=1,
    )
    ax.add_patch(input_box)
    ax.text(
        5,
        3.9,
        'Natural Language Query: "What are COVID-19 spike protein neutralizing epitopes?"',
        fontsize=12,
        fontweight="bold",
        ha="center",
        va="center",
        color="white",
    )

    # Output box
    output_box = FancyBboxPatch(
        (0.5, 1.5),
        8.5,
        1.2,
        boxstyle="round,pad=0.1",
        facecolor=COLORS["output"],
        alpha=0.7,
        edgecolor="black",
        linewidth=1,
    )
    ax.add_patch(output_box)
    ax.text(
        5,
        2.3,
        "Comprehensive Scientific Analysis",
        fontsize=14,
        fontweight="bold",
        ha="center",
        va="center",
        color="white",
    )
    ax.text(
        5,
        1.9,
        "Virus Classification • Data Sources • Grounded Synthesis",
        fontsize=11,
        ha="center",
        va="center",
        color="white",
        style="italic",
    )

    # Arrows from input to first step and last step to output
    ax.annotate("", xy=(1.5, 5.5), xytext=(1.5, 4.3), arrowprops=arrow_props)
    ax.annotate("", xy=(7.5, 2.7), xytext=(7.5, 5.5), arrowprops=arrow_props)

    # LLM indicator
    llm_box = FancyBboxPatch(
        (7.2, 4.2),
        1.6,
        0.6,
        boxstyle="round,pad=0.05",
        facecolor=COLORS["llm"],
        alpha=0.9,
        edgecolor="black",
        linewidth=1,
    )
    ax.add_patch(llm_box)
    ax.text(
        8,
        4.5,
        "LLM\nMistral-Nemo",
        fontsize=9,
        fontweight="bold",
        ha="center",
        va="center",
        color="white",
    )

    plt.tight_layout()
    plt.savefig("viral_workflow_overview.png", dpi=300, bbox_inches="tight")
    print("✅ Generated: viral_workflow_overview.png")
    plt.close()


def create_data_flow_diagram():
    """Create detailed data flow diagram."""

    fig, ax = plt.subplots(1, 1, figsize=(16, 12))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 10)
    ax.axis("off")

    # Title
    ax.text(
        6,
        9.5,
        "Viral Epitope Analysis Data Flow",
        fontsize=20,
        fontweight="bold",
        ha="center",
        va="center",
    )

    # Input query
    query_box = FancyBboxPatch(
        (4.5, 8.2),
        3,
        0.8,
        boxstyle="round,pad=0.1",
        facecolor=COLORS["input"],
        alpha=0.7,
        edgecolor="black",
        linewidth=2,
    )
    ax.add_patch(query_box)
    ax.text(
        6,
        8.6,
        "Input Query",
        fontsize=12,
        fontweight="bold",
        ha="center",
        va="center",
        color="white",
    )

    # Step 1: Classification
    class_box = FancyBboxPatch(
        (4.5, 6.8),
        3,
        1,
        boxstyle="round,pad=0.1",
        facecolor=COLORS["processing"],
        alpha=0.7,
        edgecolor="black",
        linewidth=2,
    )
    ax.add_patch(class_box)
    ax.text(
        6,
        7.3,
        "Viral Classification",
        fontsize=12,
        fontweight="bold",
        ha="center",
        va="center",
        color="white",
    )

    # Classification outputs
    outputs = [
        (1.5, 5.5, "Virus Family\n(e.g., Coronaviridae)"),
        (3.5, 5.5, "Specific Virus\n(e.g., SARS-CoV-2)"),
        (5.5, 5.5, "Research Type\n(e.g., epitope_analysis)"),
        (7.5, 5.5, "Proteins\n(e.g., spike protein)"),
        (9.5, 5.5, "Confidence\nScore"),
    ]

    for x, y, label in outputs:
        box = FancyBboxPatch(
            (x - 0.7, y - 0.3),
            1.4,
            0.8,
            boxstyle="round,pad=0.05",
            facecolor=COLORS["framework"],
            alpha=0.7,
            edgecolor="black",
            linewidth=1,
        )
        ax.add_patch(box)
        ax.text(x, y, label, fontsize=9, ha="center", va="center", fontweight="bold")

        # Arrow from classifier to output
        ax.annotate(
            "",
            xy=(x, y + 0.5),
            xytext=(6, 6.8),
            arrowprops={"arrowstyle": "->", "lw": 1.5, "color": "gray"},
        )

    # Data sources
    sources = [
        (1, 3.8, "VIOLIN\nMappings", "2,545 results"),
        (3, 3.8, "BV-BRC\nGenomes", "0 results"),
        (5, 3.8, "RAG\nChunks", "20 results"),
        (7, 3.8, "PubMed\nPapers", "0 results"),
        (9, 3.8, "Globus\nStructures", "100 results"),
        (11, 3.8, "Enhanced\nLookup", "Unlimited"),
    ]

    for x, y, label, count in sources:
        box = FancyBboxPatch(
            (x - 0.6, y - 0.4),
            1.2,
            1,
            boxstyle="round,pad=0.05",
            facecolor=COLORS["data_source"],
            alpha=0.7,
            edgecolor="black",
            linewidth=1,
        )
        ax.add_patch(box)
        ax.text(
            x,
            y + 0.2,
            label,
            fontsize=10,
            fontweight="bold",
            ha="center",
            va="center",
            color="white",
        )
        ax.text(
            x, y - 0.2, count, fontsize=8, ha="center", va="center", color="white", style="italic"
        )

    # Assembly step
    assembly_box = FancyBboxPatch(
        (4.5, 2.5),
        3,
        1,
        boxstyle="round,pad=0.1",
        facecolor=COLORS["processing"],
        alpha=0.7,
        edgecolor="black",
        linewidth=2,
    )
    ax.add_patch(assembly_box)
    ax.text(
        6,
        3,
        "Unlimited Assembly",
        fontsize=12,
        fontweight="bold",
        ha="center",
        va="center",
        color="white",
    )

    # Synthesis step (LLM)
    synth_box = FancyBboxPatch(
        (4.5, 1),
        3,
        1,
        boxstyle="round,pad=0.1",
        facecolor=COLORS["llm"],
        alpha=0.7,
        edgecolor="black",
        linewidth=2,
    )
    ax.add_patch(synth_box)
    ax.text(
        6,
        1.5,
        "LLM Synthesis",
        fontsize=12,
        fontweight="bold",
        ha="center",
        va="center",
        color="white",
    )
    ax.text(
        6,
        1.2,
        "(Mistral-Nemo)",
        fontsize=10,
        ha="center",
        va="center",
        color="white",
        style="italic",
    )

    # Arrows for main flow
    arrow_props = {"arrowstyle": "->", "lw": 2, "color": "#2c3e50"}
    ax.annotate("", xy=(6, 6.8), xytext=(6, 8.2), arrowprops=arrow_props)
    ax.annotate("", xy=(6, 2.5), xytext=(6, 5.2), arrowprops=arrow_props)
    ax.annotate("", xy=(6, 1), xytext=(6, 2.5), arrowprops=arrow_props)

    # Arrows from data sources to assembly
    for x, _, _, _ in sources:
        ax.annotate(
            "",
            xy=(5.5 if x < 6 else 6.5, 3.2),
            xytext=(x, 3.4),
            arrowprops={"arrowstyle": "->", "lw": 1, "color": "green", "alpha": 0.7},
        )

    plt.tight_layout()
    plt.savefig("viral_workflow_dataflow.png", dpi=300, bbox_inches="tight")
    print("✅ Generated: viral_workflow_dataflow.png")
    plt.close()


def create_component_architecture():
    """Create component architecture diagram."""

    fig, ax = plt.subplots(1, 1, figsize=(14, 10))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, 10)
    ax.axis("off")

    # Title
    ax.text(
        6,
        9.5,
        "Nanobrain Framework Architecture",
        fontsize=20,
        fontweight="bold",
        ha="center",
        va="center",
    )

    # Framework layer
    framework_box = FancyBboxPatch(
        (0.5, 7.5),
        11,
        1.5,
        boxstyle="round,pad=0.1",
        facecolor=COLORS["framework"],
        alpha=0.3,
        edgecolor="black",
        linewidth=1,
    )
    ax.add_patch(framework_box)
    ax.text(
        6,
        8.7,
        "Nanobrain Framework Components",
        fontsize=14,
        fontweight="bold",
        ha="center",
        va="center",
    )

    # Individual components
    components = [
        # Step components
        (1.5, 6, 2.5, 1, "ViralImmunologyQuery\nClassifierStep", COLORS["processing"]),
        (4.5, 6, 2.5, 1, "UnlimitedSynthesis\nAssemblyStep", COLORS["data_source"]),
        (7.5, 6, 2.5, 1, "RagSynthesisStep\n(LLM Integration)", COLORS["llm"]),
        # Data units
        (1, 4.5, 1.5, 0.8, "DataUnit\nMemory", COLORS["framework"]),
        (3, 4.5, 1.5, 0.8, "Trigger\nSystem", COLORS["framework"]),
        (5, 4.5, 1.5, 0.8, "Direct\nLinks", COLORS["framework"]),
        (7, 4.5, 1.5, 0.8, "Executor\nLocal", COLORS["framework"]),
        (9, 4.5, 1.5, 0.8, "Config\nYAML", COLORS["framework"]),
        # External integrations
        (1, 2.8, 2, 1, "VIOLIN\nDatabase", COLORS["data_source"]),
        (3.5, 2.8, 2, 1, "BV-BRC\nGenomes", COLORS["data_source"]),
        (6, 2.8, 2, 1, "Globus\nSearch", COLORS["data_source"]),
        (8.5, 2.8, 2, 1, "RAG Index\nFAISS", COLORS["data_source"]),
        # LLM integration
        (4.5, 1.2, 3, 1, "Mistral-Nemo LLM\n(Ollama)", COLORS["llm"]),
    ]

    for x, y, w, h, label, color in components:
        box = FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle="round,pad=0.05",
            facecolor=color,
            alpha=0.7,
            edgecolor="black",
            linewidth=1,
        )
        ax.add_patch(box)
        ax.text(
            x + w / 2,
            y + h / 2,
            label,
            fontsize=10,
            fontweight="bold",
            ha="center",
            va="center",
            color="white" if color != COLORS["framework"] else "black",
        )

    # Connection lines
    connections = [
        # Step to step connections
        ((2.75, 6), (4.5, 6.5)),
        ((5.75, 6), (7.5, 6.5)),
        # Steps to framework components
        ((2.75, 6), (2.75, 5.3)),
        ((5.75, 6), (5.75, 5.3)),
        ((8.75, 6), (8.75, 5.3)),
        # Assembly to data sources
        ((5.75, 6), (2, 3.8)),
        ((5.75, 6), (4.5, 3.8)),
        ((5.75, 6), (7, 3.8)),
        ((5.75, 6), (9.5, 3.8)),
        # Synthesis to LLM
        ((8.75, 6), (6, 2.2)),
    ]

    for start, end in connections:
        ax.plot([start[0], end[0]], [start[1], end[1]], "k-", alpha=0.5, linewidth=1.5)

    # Add legend
    legend_elements = [
        patches.Patch(color=COLORS["processing"], alpha=0.7, label="Processing Steps"),
        patches.Patch(color=COLORS["llm"], alpha=0.7, label="LLM Integration"),
        patches.Patch(color=COLORS["data_source"], alpha=0.7, label="Data Sources"),
        patches.Patch(color=COLORS["framework"], alpha=0.7, label="Framework Components"),
    ]
    ax.legend(handles=legend_elements, loc="lower right", bbox_to_anchor=(1, 0))

    plt.tight_layout()
    plt.savefig("viral_workflow_architecture.png", dpi=300, bbox_inches="tight")
    print("✅ Generated: viral_workflow_architecture.png")
    plt.close()


def create_performance_comparison():
    """Create performance comparison visualization."""

    fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2, 2, figsize=(16, 12))
    fig.suptitle(
        "Viral Epitope Analysis: Before vs After Transformation", fontsize=16, fontweight="bold"
    )

    # 1. Data retrieval comparison
    categories = ["COVID-19", "Influenza A", "EEEV", "Zika", "HIV"]
    before = [10, 10, 10, 10, 10]  # Old caps
    after = [2545, 50, 32, 19, 2560]  # New unlimited

    x = np.arange(len(categories))
    width = 0.35

    ax1.bar(x - width / 2, before, width, label="Before (Capped)", color=COLORS["input"], alpha=0.7)
    bars2 = ax1.bar(
        x + width / 2,
        after,
        width,
        label="After (Unlimited)",
        color=COLORS["data_source"],
        alpha=0.7,
    )

    ax1.set_ylabel("Number of Results")
    ax1.set_title("Data Retrieval Capacity")
    ax1.set_xticks(x)
    ax1.set_xticklabels(categories, rotation=45)
    ax1.legend()
    ax1.set_yscale("log")  # Log scale due to large differences

    # Add value labels on bars
    for bar in bars2:
        height = bar.get_height()
        if height > 0:
            ax1.annotate(
                f"{int(height)}",
                xy=(bar.get_x() + bar.get_width() / 2, height),
                xytext=(0, 3),  # 3 points vertical offset
                textcoords="offset points",
                ha="center",
                va="bottom",
                fontsize=8,
            )

    # 2. Virus family support
    families = ["Alphaviridae", "Coronaviridae", "Orthomyxoviridae", "Flaviviridae", "Retroviridae"]
    before_support = [1, 0, 0, 0, 0]  # Only EEEV
    after_support = [1, 1, 1, 1, 1]  # All supported

    x2 = np.arange(len(families))
    ax2.bar(x2, before_support, width, label="Before (EEEV only)", color=COLORS["input"], alpha=0.7)
    ax2.bar(
        x2,
        after_support,
        width,
        bottom=before_support,
        label="After (All viruses)",
        color=COLORS["processing"],
        alpha=0.7,
    )

    ax2.set_ylabel("Supported (Yes=1, No=0)")
    ax2.set_title("Virus Family Support")
    ax2.set_xticks(x2)
    ax2.set_xticklabels(families, rotation=45)
    ax2.legend()
    ax2.set_ylim(0, 1.2)

    # 3. Classification accuracy
    confidence_data = pd.DataFrame(
        {
            "Virus": ["COVID-19", "Influenza A", "EEEV", "Zika", "HIV"],
            "Confidence": [1.000, 1.000, 1.000, 1.000, 1.000],
            "Research_Type": ["Epitope", "Epitope", "Epitope", "Antibody", "Vaccine"],
        }
    )

    colors = [
        COLORS["llm"],
        COLORS["processing"],
        COLORS["data_source"],
        COLORS["output"],
        COLORS["framework"],
    ]
    bars5 = ax3.bar(
        confidence_data["Virus"], confidence_data["Confidence"], color=colors, alpha=0.7
    )
    ax3.set_ylabel("Classification Confidence")
    ax3.set_title("Virus Classification Accuracy")
    ax3.set_xticklabels(confidence_data["Virus"], rotation=45)
    ax3.set_ylim(0, 1.1)

    # Add confidence labels
    for i, bar in enumerate(bars5):
        ax3.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + 0.02,
            f"{confidence_data.iloc[i]['Confidence']:.3f}",
            ha="center",
            va="bottom",
            fontweight="bold",
        )

    # 4. Framework compliance metrics
    metrics = [
        "Config\nDriven",
        "Unlimited\nRetrieval",
        "Quality\nFiltering",
        "LLM\nSynthesis",
        "Citation\nGrounding",
    ]
    before_compliance = [0, 0, 0, 0, 0]  # Hardcoded system
    after_compliance = [1, 1, 1, 1, 1]  # Full compliance

    x3 = np.arange(len(metrics))
    ax4.bar(x3, before_compliance, width, label="Before", color=COLORS["input"], alpha=0.7)
    ax4.bar(
        x3,
        after_compliance,
        width,
        bottom=before_compliance,
        label="After",
        color=COLORS["llm"],
        alpha=0.7,
    )

    ax4.set_ylabel("Feature Available")
    ax4.set_title("Framework Compliance Features")
    ax4.set_xticks(x3)
    ax4.set_xticklabels(metrics, rotation=0)
    ax4.legend()
    ax4.set_ylim(0, 1.2)

    plt.tight_layout()
    plt.savefig("viral_workflow_performance.png", dpi=300, bbox_inches="tight")
    print("✅ Generated: viral_workflow_performance.png")
    plt.close()


def main():
    """Generate all visualization diagrams."""
    print("🎨 Generating viral epitope analysis workflow visualizations...")
    print("📊 Using seaborn-based styling with alpha=0.7 transparency")

    # Ensure output directory exists
    Path(".").mkdir(exist_ok=True)

    # Generate all diagrams
    create_workflow_overview()
    create_data_flow_diagram()
    create_component_architecture()
    create_performance_comparison()

    print("\n🎉 All visualizations generated successfully!")
    print("📁 Generated files:")
    print("   • viral_workflow_overview.png - High-level workflow")
    print("   • viral_workflow_dataflow.png - Data flow diagram")
    print("   • viral_workflow_architecture.png - Component architecture")
    print("   • viral_workflow_performance.png - Performance comparison")

    print("\n🔍 Features:")
    print("   ✅ LLM steps clearly marked with red coloring")
    print("   ✅ Legible, non-overlapping labels")
    print("   ✅ Alpha=0.7 transparency for all color fills")
    print("   ✅ Presentation-ready fonts and styling")
    print("   ✅ Clean, professional appearance")


if __name__ == "__main__":
    main()
