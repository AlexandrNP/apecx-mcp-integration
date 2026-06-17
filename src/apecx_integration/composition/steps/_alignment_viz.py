"""Sequence-conservation visualization helpers for the viral_epitope_analysis report.

Two renderers, both PURE (no step/framework state) so they unit-test without a workflow:

* ``render_conservation_text`` — a dependency-free, ALWAYS-available inline text track
  (consensus motif + a per-column identity sparkline per conserved region). This is the
  degrade-loud floor: the report always carries *some* conservation visualization.
* ``render_conservation_png`` — a matplotlib conservation plot (per-column identity track with
  conserved-region bands + a sequence logo of the top conserved region) written as a PNG
  artifact. matplotlib is an OPTIONAL extra (``pip install '.[viz]'``); the function
  lazy-imports it INSIDE the body and pins the headless ``Agg`` backend BEFORE pyplot, so a
  clean install without the extra (or a headless server) degrades to ``None`` + one loud log
  line — never a crash, never a broken image link.

Layering: ``_artifacts_dir`` is inlined here (NOT imported from the mcp_surface layer) — the
composition layer must not import upward; this 6-line helper is deliberately duplicated across
composition steps (cf. ``structural_reasoning_step._artifacts_dir``).
"""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path
from typing import Any

from apecx_integration.composition.steps.conservation_score_step import _parse_fasta

log = logging.getLogger(__name__)

# Block glyphs for the text-track identity sparkline (8 levels, low→high).
_SPARK = "▁▂▃▄▅▆▇█"
# Cap the logo width so a long conserved region stays a readable PNG (note the truncation).
_LOGO_MAX_COLS = 60
# Cap the text-track motif/sparkline length so the markdown stays compact.
_TEXT_MAX_COLS = 80


def _artifacts_dir() -> Path:
    """Durable artifact dir (env override or ~/.apecx/artifacts). Inlined per the layering rule
    (cf. structural_reasoning_step._artifacts_dir) — composition must not import the mcp layer."""
    base = Path(os.environ.get("APECX_ARTIFACTS_DIR") or (Path.home() / ".apecx" / "artifacts"))
    base.mkdir(parents=True, exist_ok=True)
    return base


def _spark(values: list[float]) -> str:
    """Render a 0..1 value series as a block-glyph sparkline (degrade-loud on empty)."""
    if not values:
        return ""
    out = []
    for v in values:
        v = 0.0 if v is None else max(0.0, min(1.0, float(v)))
        out.append(_SPARK[min(len(_SPARK) - 1, int(v * (len(_SPARK) - 1) + 0.5))])
    return "".join(out)


def render_conservation_text(
    per_column: list[dict[str, Any]] | None,
    conserved_regions: list[dict[str, Any]] | None,
    *,
    protein: str = "protein",
    n_sequences: int | None = None,
) -> str:
    """Dependency-free inline conservation track (the degrade-loud floor). Returns a markdown
    fragment; returns a loud one-liner when there's nothing to show."""
    regions = conserved_regions or []
    if not regions:
        return "_No conserved regions were found to visualize._"
    pc = per_column or []
    n = f", {n_sequences} strains" if n_sequences else ""
    lines = [f"Conserved regions of {protein}{n} (consensus motif + per-column identity):", ""]
    lines.append("```")
    for i, r in enumerate(regions, 1):
        start, end = int(r.get("start", 0)), int(r.get("end", 0))
        motif = str(r.get("consensus") or "")
        mean_id = r.get("mean_identity")
        # Per-column identities within the region (from per_column when present).
        ids = [
            float(pc[c].get("identity", 0.0))
            for c in range(start, end + 1)
            if 0 <= c < len(pc) and isinstance(pc[c], dict)
        ]
        trunc = "…" if len(motif) > _TEXT_MAX_COLS else ""
        spark = _spark(ids[:_TEXT_MAX_COLS])
        head = f"region {i}  cols {start}-{end}  len {end - start + 1}"
        if isinstance(mean_id, (int, float)):
            head += f"  mean id {mean_id:.3f}"
        lines.append(head)
        lines.append(f"  {motif[:_TEXT_MAX_COLS]}{trunc}")
        if spark:
            lines.append(f"  {spark}{trunc}")
    lines.append("```")
    return "\n".join(lines)


def _region_letter_freqs(
    seqs: list[tuple[str, str]], start: int, end: int
) -> list[list[tuple[str, float]]]:
    """Per-column (residue, information-bits height) for a logo over alignment cols [start, end].

    Information content per column = log2(20) - Shannon entropy (bits); each residue's letter
    height = freq * IC. Gaps are excluded from the frequency base."""
    cols: list[list[tuple[str, float]]] = []
    for c in range(start, end + 1):
        counts: dict[str, int] = {}
        total = 0
        for _, s in seqs:
            if c < len(s):
                ch = s[c]
                if ch != "-":
                    counts[ch] = counts.get(ch, 0) + 1
                    total += 1
        if total == 0:
            cols.append([])
            continue
        freqs = {ch: n / total for ch, n in counts.items()}
        entropy = -sum(f * math.log2(f) for f in freqs.values() if f > 0)
        ic = max(0.0, math.log2(20) - entropy)
        # ascending height so the tallest (most frequent) letter stacks on top
        cols.append(sorted(((ch, f * ic) for ch, f in freqs.items()), key=lambda t: t[1]))
    return cols


def render_conservation_png(
    per_column: list[dict[str, Any]] | None,
    conserved_regions: list[dict[str, Any]] | None,
    alignment_fasta: str | None,
    *,
    protein: str = "protein",
    n_sequences: int | None = None,
    dest_dir: Path | None = None,
    basename: str = "conservation",
) -> str | None:
    """Render a conservation plot PNG; return its BASENAME (relative, co-located with the report
    .md) or ``None`` on any degrade (matplotlib missing, no data, render error). DEGRADE-LOUD:
    every failure path logs a reason — the caller renders the text track instead, never a broken
    image. matplotlib is lazy-imported here so a clean install without the ``viz`` extra still
    runs the workflow."""
    pc = per_column or []
    regions = conserved_regions or []
    if not pc:
        log.warning("render_conservation_png: no per_column data — falling back to text track.")
        return None

    try:
        import matplotlib

        matplotlib.use("Agg")  # headless: MUST be set before importing pyplot
        import matplotlib.pyplot as plt
    except ImportError:
        log.warning(
            "render_conservation_png: matplotlib not installed — install '.[viz]' for the "
            "conservation PNG; rendering the inline text track instead."
        )
        return None

    try:
        identities = [float(c.get("identity", 0.0)) for c in pc if isinstance(c, dict)]
        n_cols = len(identities)
        # Pick the top conserved region for the logo (longest, tie-broken by mean identity).
        logo_region = max(
            regions,
            key=lambda r: (
                int(r.get("end", 0)) - int(r.get("start", 0)),
                r.get("mean_identity", 0),
            ),
            default=None,
        )
        seqs = _parse_fasta(alignment_fasta) if alignment_fasta else []

        have_logo = bool(logo_region and seqs)
        fig, axes = plt.subplots(
            2 if have_logo else 1,
            1,
            figsize=(11, 5.5 if have_logo else 3.2),
            gridspec_kw={"height_ratios": [2, 1.4]} if have_logo else None,
        )
        ax_track = axes[0] if have_logo else axes

        # --- identity track ---
        ax_track.fill_between(range(n_cols), identities, color="#4C72B0", alpha=0.85, linewidth=0)
        for r in regions:
            ax_track.axvspan(
                int(r.get("start", 0)), int(r.get("end", 0)), color="#DD8452", alpha=0.25
            )
        ax_track.set_ylim(0, 1.02)
        ax_track.set_xlim(0, max(1, n_cols - 1))
        ax_track.set_ylabel("identity")
        ttl = f"Per-column conservation — {protein}"
        if n_sequences:
            ttl += f" ({n_sequences} strains, {len(regions)} conserved regions)"
        ax_track.set_title(ttl, fontsize=11)
        ax_track.set_xlabel("alignment column")

        # --- sequence logo of the top conserved region ---
        if have_logo:
            ax_logo = axes[1]
            start = int(logo_region.get("start", 0))
            end = int(logo_region.get("end", 0))
            truncated = (end - start + 1) > _LOGO_MAX_COLS
            if truncated:
                end = start + _LOGO_MAX_COLS - 1
            cols = _region_letter_freqs(seqs, start, end)
            max_ic = max((sum(h for _, h in col) for col in cols), default=1.0) or 1.0
            for x, col in enumerate(cols):
                y = 0.0
                for ch, h in col:
                    if h <= 0:
                        continue
                    ax_logo.text(
                        x + 0.5,
                        y + h / 2,
                        ch,
                        ha="center",
                        va="center",
                        fontsize=max(4, min(18, 18 * h / max_ic)),
                        fontfamily="monospace",
                        fontweight="bold",
                    )
                    y += h
            ax_logo.set_xlim(0, max(1, len(cols)))
            ax_logo.set_ylim(0, max_ic * 1.05)
            ax_logo.set_yticks([])
            ax_logo.set_xlabel(
                f"top conserved region cols {start}-{end}"
                + (" (truncated)" if truncated else "")
                + " — sequence logo (letter height = information content)"
            )

        fig.tight_layout()
        dest = (dest_dir or _artifacts_dir()) / f"{basename}.png"
        fig.savefig(dest, dpi=120, bbox_inches="tight")
        plt.close(fig)
        if dest.exists() and dest.stat().st_size > 0:
            return dest.name
        log.warning("render_conservation_png: savefig produced no file at %s.", dest)
        return None
    except Exception as exc:  # noqa: BLE001 — viz is best-effort; never break the report
        log.warning("render_conservation_png: render failed (%s: %s).", type(exc).__name__, exc)
        return None


__all__ = ["render_conservation_png", "render_conservation_text"]
