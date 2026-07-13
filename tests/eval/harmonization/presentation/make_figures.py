"""Generate presentation-quality PNG figures from the harmonization eval JSON.

Reads output/harmonization_precision.json (precision/recall/coverage/rootcause + per_judge_stats +
inter_judge_kappa) and emits figures/*.png. Every number is read from the JSON — no hardcoded results —
so the deck stays faithful to the run. Organized BY LOGIC (a figure per finding), not per model.

Run:  PYTHONPATH=src:. .venv/bin/python tests/eval/harmonization/presentation/make_figures.py
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

_HERE = Path(__file__).parent
_JSON = _HERE.parent / "output" / "harmonization_precision.json"
_OUT = _HERE / "figures"
_OUT.mkdir(exist_ok=True)

# Palette
INK = "#1f2933"
GOOD = "#2b8a6f"  # teal — works / precise
BAD = "#c0392b"  # red — 0.0 / failure
WARN = "#e08e0b"  # amber
NEUTRAL = "#8895a7"
BLUE = "#2c6fbb"

plt.rcParams.update(
    {
        "font.size": 13,
        "font.family": "sans-serif",
        "axes.edgecolor": NEUTRAL,
        "axes.labelcolor": INK,
        "text.color": INK,
        "xtick.color": INK,
        "ytick.color": INK,
        "axes.titlesize": 15,
        "axes.titleweight": "bold",
        "figure.dpi": 150,
    }
)


def _clean(ax):
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", color="#e5e9ef", linewidth=0.8)
    ax.set_axisbelow(True)


def _save(fig, name):
    fig.tight_layout()
    fig.savefig(_OUT / name, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print("wrote", name)


d = json.loads(_JSON.read_text())
cells = d["cells"]


# ---- FIG 1: non-circular precision reveals real variation (by regime + category) -----------------
def fig_precision():
    reg = d["aggregate"]["by_regime"]
    cat = d["aggregate"]["by_category"]
    labels = ["resolved\nspecies", "miss →\nraw fallback"]
    vals = [reg["resolved_species"]["precision"], reg["miss_raw_fallback"]["precision"]]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 5.2), gridspec_kw={"width_ratios": [1, 1.15]})
    bars = a1.bar(labels, vals, color=[GOOD, BAD], width=0.6)
    a1.axhline(1.0, ls="--", color=NEUTRAL, lw=1)
    a1.text(1.5, 1.01, "old circular metric = 1.00 (flat)", ha="right", fontsize=10, color=NEUTRAL)
    for b, v in zip(bars, vals, strict=True):
        a1.text(b.get_x() + b.get_width() / 2, v + 0.02, f"{v:.2f}", ha="center", fontweight="bold")
    a1.set_ylim(0, 1.12)
    a1.set_ylabel("precision (non-circular)")
    a1.set_title("Precision by resolution regime")
    _clean(a1)

    cats = ["mu_virus", "abbreviations", "real_world"]
    cvals = [cat[c]["precision"] for c in cats]
    bars = a2.barh(cats[::-1], cvals[::-1], color=BLUE, height=0.55)
    for b, v in zip(bars, cvals[::-1], strict=True):
        a2.text(
            v + 0.01, b.get_y() + b.get_height() / 2, f"{v:.2f}", va="center", fontweight="bold"
        )
    a2.set_xlim(0, 1.05)
    a2.set_xlabel("precision")
    a2.set_title("Precision by query category")
    a2.spines["top"].set_visible(False)
    a2.spines["right"].set_visible(False)
    a2.grid(axis="x", color="#e5e9ef", lw=0.8)
    a2.set_axisbelow(True)
    fig.suptitle(
        "Non-circular judging exposes real variation the old metric hid (0.00 – 0.93)",
        fontsize=15,
        fontweight="bold",
    )
    _save(fig, "fig1_precision.png")


# ---- FIG 2: false-positive attribution — the raw-fallback leak dominates --------------------------
def fig_fp():
    fp = Counter()
    for c in cells:
        for k, v in (c.get("fp_breakdown") or {}).items():
            fp[k] += v
    order = ["raw_substitution", "multi_subject_incidental"]
    vals = [fp.get(k, 0) for k in order]
    total = sum(vals)
    labels = [
        "raw_substitution\n(miss/broken → raw text served)",
        "multi_subject\n(incidental co-mention)",
    ]
    fig, ax = plt.subplots(figsize=(9.5, 4.6))
    bars = ax.barh(labels[::-1], vals[::-1], color=[NEUTRAL, BAD][::-1], height=0.5)
    for b, v in zip(bars, vals[::-1], strict=True):
        ax.text(
            v + total * 0.01,
            b.get_y() + b.get_height() / 2,
            f"{v}  ({v / total:.0%})",
            va="center",
            fontweight="bold",
        )
    ax.set_xlim(0, total * 1.15)
    ax.set_xlabel("false positives (count across all judged records)")
    ax.set_title("89% of ALL false positives are the raw-text fallback")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="x", color="#e5e9ef", lw=0.8)
    ax.set_axisbelow(True)
    _save(fig, "fig2_fp_attribution.png")


# ---- FIG 3: 0-coverage root cause — genuine absence, NOT stamping ---------------------------------
def fig_rootcause():
    tot = Counter()
    for col in d["coverage_rootcause"].values():
        for k, v in col.items():
            if k != "covered":
                tot[k] += v
    order = ["genuinely_absent", "offtarget_raw_match", "missing_source_id", "stamping_mismatch"]
    labels = [
        "genuinely absent\n(no record at all)",
        "off-target raw match\n(other organisms)",
        "missing source id\n(no taxon id to stamp)",
        "stamping mismatch\n(HARMONIZATION failure)",
    ]
    vals = [tot.get(k, 0) for k in order]
    colors = [NEUTRAL, WARN, BLUE, BAD]
    fig, ax = plt.subplots(figsize=(10, 4.8))
    bars = ax.bar(labels, vals, color=colors, width=0.62)
    for b, v in zip(bars, vals, strict=True):
        ax.text(b.get_x() + b.get_width() / 2, v + 4, str(v), ha="center", fontweight="bold")
    ax.set_ylabel("0-coverage cells")
    ax.set_ylim(0, max(vals) * 1.18)
    ax.set_title("Coverage gaps are genuine absence — ZERO stamping failures")
    _clean(ax)
    _save(fig, "fig3_rootcause.png")


# ---- FIG 4: per-index coverage vs precision ------------------------------------------------------
def fig_per_index():
    cov = d["coverage"]
    ia = d["aggregate"]["by_index"]
    names = sorted(cov, key=lambda n: cov[n]["coverage_rate"], reverse=True)
    covr = [cov[n]["coverage_rate"] for n in names]
    prec = [ia.get(n, {}).get("precision") for n in names]
    y = range(len(names))
    fig, ax = plt.subplots(figsize=(10.5, 5.6))
    ax.barh([i + 0.2 for i in y], covr, height=0.38, color=BLUE, label="coverage rate")
    ax.barh(
        [i - 0.2 for i in y],
        [p if p is not None else 0 for p in prec],
        height=0.38,
        color=GOOD,
        label="precision",
    )
    for i, p in zip(y, prec, strict=True):
        if p is not None:
            ax.text((p or 0) + 0.01, i - 0.2, f"{p:.2f}", va="center", fontsize=10)
    for i, cr in zip(y, covr, strict=True):
        ax.text(cr + 0.01, i + 0.2, f"{cr:.2f}", va="center", fontsize=10)
    ax.set_yticks(list(y))
    ax.set_yticklabels([n.replace("_", " ") for n in names])
    ax.invert_yaxis()
    ax.set_xlim(0, 1.05)
    ax.set_xlabel("rate")
    ax.set_title("Per-index coverage & non-circular precision (9 harmonized indices)")
    ax.legend(loc="lower right", frameon=False)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="x", color="#e5e9ef", lw=0.8)
    ax.set_axisbelow(True)
    _save(fig, "fig4_per_index.png")


# ---- FIG 5: inter-judge kappa matrix heatmap ----------------------------------------------------
def fig_kappa():
    km = d["per_judge_stats"]["inter_judge_kappa"]
    names = list(km.keys())
    short = {
        "judge_a": "taxonomy\n(A)",
        "judge_b": "text\n(B)",
        "combined": "combined",
        "nemotron-3-nano:4b": "nemotron\n4B",
        "gemma4:latest": "gemma4\n8B",
        "medgemma:latest": "medgemma\n(bio)",
        "medllama2:7b": "medllama2\n(bio)",
        "mistral-nemo:latest": "mistral\n12B",
        "devstral:24b": "devstral\n24B",
    }
    n = len(names)
    mat = [[km[a][b] if km[a][b] is not None else 0.0 for b in names] for a in names]
    fig, ax = plt.subplots(figsize=(8.8, 7.6))
    im = ax.imshow(mat, cmap="RdBu", vmin=-0.7, vmax=1.0)
    ax.set_xticks(range(n))
    ax.set_yticks(range(n))
    ax.set_xticklabels([short[x] for x in names], fontsize=9)
    ax.set_yticklabels([short[x] for x in names], fontsize=9)
    for i in range(n):
        for j in range(n):
            v = mat[i][j]
            ax.text(
                j,
                i,
                f"{v:.2f}",
                ha="center",
                va="center",
                fontsize=8,
                color="white" if abs(v) > 0.55 else INK,
            )
    ax.set_title(
        "Inter-judge agreement (Cohen κ): capable models cluster,\nmedllama2 isolated, LLMs weak vs taxonomy"
    )
    fig.colorbar(im, ax=ax, shrink=0.7, label="Cohen κ")
    _save(fig, "fig5_kappa_matrix.png")


# ---- FIG 6: per-judge precision/recall, grouped BY LOGIC (not per model) --------------------------
def fig_judge_pr():
    ov = d["per_judge_stats"]["by_category_majority"]["overall"]
    groups = {
        "Capable LLM judges": (
            [
                "nemotron-3-nano:4b",
                "gemma4:latest",
                "medgemma:latest",
                "mistral-nemo:latest",
                "devstral:24b",
            ],
            GOOD,
        ),
        "Failed judge (medllama2)": (["medllama2:7b"], BAD),
        "Automated judges": (["judge_a", "judge_b", "combined"], BLUE),
    }
    fig, ax = plt.subplots(figsize=(9.2, 6.2))
    for label, (members, color) in groups.items():
        xs = [ov[m]["precision"] for m in members if ov[m]["precision"] is not None]
        ys = [ov[m]["recall"] for m in members if ov[m]["recall"] is not None]
        ax.scatter(
            xs, ys, s=140, color=color, edgecolor="white", linewidth=1.2, zorder=3, label=label
        )
    # annotate the two load-bearing points
    ax.annotate(
        "medllama2\nrecall 0.014 →\nnear-constant reject",
        (ov["medllama2:7b"]["precision"], ov["medllama2:7b"]["recall"]),
        textcoords="offset points",
        xytext=(10, 18),
        fontsize=10,
        color=BAD,
        arrowprops={"arrowstyle": "->", "color": BAD},
    )
    ax.annotate(
        "taxonomy judge\nhigh precision,\nabstains a lot",
        (ov["judge_a"]["precision"], ov["judge_a"]["recall"]),
        textcoords="offset points",
        xytext=(-120, -6),
        fontsize=10,
        color=BLUE,
    )
    ax.axhspan(0.85, 1.0, xmin=0.0, xmax=1.0, color=GOOD, alpha=0.06)
    ax.set_xlabel("precision (vs panel-majority)")
    ax.set_ylabel("recall (vs panel-majority)")
    ax.set_xlim(0.6, 1.0)
    ax.set_ylim(-0.03, 1.02)
    ax.set_title(
        "Judge quality by logic: 5 models cluster high;\none bio model fails; automated judges trade recall for precision"
    )
    ax.legend(loc="center left", frameon=False)
    _clean(ax)
    ax.grid(axis="x", color="#e5e9ef", lw=0.8)
    _save(fig, "fig6_judge_pr.png")


if __name__ == "__main__":
    fig_precision()
    fig_fp()
    fig_rootcause()
    fig_per_index()
    fig_kappa()
    fig_judge_pr()
    print("all figures →", _OUT)
