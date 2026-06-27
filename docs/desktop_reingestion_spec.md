# Spec — desktop workflow output engineered for re-ingestion by a WEAK host LLM (Haiku)

Status: SPEC (ready to implement). Branch off `main` (`6201506`): `desktop-reingestion-output`.

## Problem (user, 2026-06-27)
In desktop mode the host LLM (Claude Desktop, expect a WEAK model — Haiku) RE-INGESTS the
run_workflow result and re-renders it for the user. Today it gets markdown ONLY and **crops it**;
generated **images (PyMOL surface render, conservation plot) never reach the user** (they are
file PATHS the host can't read), and there are **no instructions** telling the weak model to
present the full report + include the figures. We must engineer the output for re-ingestion.

## Mechanism (confirmed by investigation)
- FastMCP auto-converts a tool's return: `Image` (`mcp.server.fastmcp.Image` /
  `mcp.server.fastmcp.utilities.types.Image`) → `ImageContent` (base64) which **Claude Desktop
  renders inline**; a `list` return is flattened (`str`→TextContent, `Image`→ImageContent). Source:
  `.venv/.../mcp/server/fastmcp/utilities/func_metadata.py::_convert_to_content` + `types.Image`.
- Today every desktop tool call converges at
  `eo_primitives._run_workflow_streaming_impl(...)` → returns the result **dict** (markdown + paths).
  Catalog tools reach it via `workflow_registry._run_via_run_workflow` (ctx present) →
  `_live_dispatch` (`_register_one_entry`); the generic `run_workflow` tool reaches it directly.
- Images on disk: `~/.apecx/artifacts/<run_id>/figures/*.png` (PyMOL `{pdb}.png` 900x700;
  `conservation_*.png` + vector `.pdf`). The result's `artifacts` manifest carries `{name, path,
  kind}` with `kind=="figure"` for these. Structured data: `result["data_preview"]` +
  `~/.apecx/artifacts/<run_id>/data.json` (full) + `tool_outputs/*.json` (embedded text <=64KB in
  the manifest). `result["markdown"]` already has inline `![..](figures/x.png)` refs (relative).

## Design — a desktop-locus tool-boundary adapter (internal dict UNCHANGED)
Add to `eo_primitives.py` (NOT inside `_run_workflow_streaming_impl`, whose dict return is pinned by
`tests/unit/test_eo_primitives.py`):

1. `HOST_INSTRUCTIONS: str` — an explicit, simple-for-Haiku preamble, e.g.:
   - "You are presenting a scientific analysis to the user. Below is the COMPLETE report. Present it
     IN FULL — do NOT summarize, shorten, or omit sections. Reproduce every heading and its content."
   - "Figures are attached as image content blocks. INCLUDE each image inline in your reply, next to
     the section it belongs to (the epitope surface map under Structural evidence; the conservation
     plot under the conservation/breadth section)."
   - "Keep every citation/DOI exactly as written. Use the STRUCTURED DATA block for precise numbers
     (SASA exposed residues, conserved regions, counts) — do not invent or round them."
   - "Do not add disclaimers or meta-commentary; present the report as the answer."
2. `def _desktop_host_payload(result: dict) -> list[Any]`:
   - `[0]` = `f"{HOST_INSTRUCTIONS}\n\n---\n\n{result['markdown']}"` (TextContent).
   - for each `a in result.get("artifacts", [])` with `a["kind"]=="figure"` and a `.png` path that
     `Path(a["path"]).is_file()` → append `Image(path=a["path"])`. (PNG only; skip PDF — Desktop
     renders raster. Cap count/size defensively, e.g. skip >5MB.)
   - append a final TextContent = `"STRUCTURED DATA (for precise figures; do not alter):\n" +
     json.dumps(_structured_subset(result), indent=2)` where `_structured_subset` pulls
     `data_preview` + the `tool_output` manifest entries' embedded text + `run_id`/`status`/`provenance`.
   - Be degrade-loud + defensive: any Image() failure → skip that image, keep going (never break the
     tool). If no figures and no markdown, fall back to returning `result` (the dict).
3. Apply ONLY in desktop locus, at the tool boundary:
   - `_live_dispatch` (workflow_registry `_register_one_entry`): `r = await _dispatch(...);
     return _desktop_host_payload(r) if (ctx is not None and isinstance(r, dict)) else r`. (Import
     the adapter lazily to avoid a cycle.) Gate on `ctx is not None` — that IS the desktop path.
   - the generic `run_workflow` MCP tool: find its registration (grep `server.tool` + `run_workflow`
     in `mcp_surface/server.py`); wrap identically. Do NOT touch the internal `run_workflow` function
     (RunStore/tests depend on its dict).

## Tests
- `tests/unit/test_desktop_reingestion.py`: `_desktop_host_payload` on a fake result dict with a
  figure artifact (point `path` at a tmp PNG) → returns a list whose `[0]` contains HOST_INSTRUCTIONS
  + the markdown, contains an `Image` for the figure, and a final structured-data text item; a result
  with NO figures still returns instructions+markdown+structured (or the dict fallback). Assert the
  internal dict path (agent/headless / `_run_workflow_streaming_impl` return) is UNCHANGED.
- Real-data parity: run viral_epitope_analysis in DESKTOP locus (the boundary harness path) with the
  PyMOL image built → assert the tool-boundary payload carries >=1 ImageContent + the instructions.
  (Heavy; gate like the existing e2e.)

## Verification
- Build the wheel, run `scripts/validate_fresh_install.py` (delivery unaffected).
- A desktop run returns content blocks: 1 text (instructions+report) + N images + 1 structured text.
- `tests/unit/test_eo_primitives.py` stays green (internal dict return pinned — the transform is at
  the boundary, not in `_run_workflow_streaming_impl`).

## NOT in scope (this feature)
- MAFFT containerization (separate next arc).
- Streaming images mid-run (final-result images only; streaming stays progress/log notifications).
- Changing the headless/agent return shape (dict) — desktop-only.

## Other loop backlog (chained after this)
- Continue README/doc-vs-code audit; update STALE docs (code precedence). Candidates: README tool
  counts, `desktop_streaming_contract.md` / `external_orchestration_design.md` "host re-renders the
  output" sections (currently silent on rendering instructions — document the new contract).
- MAFFT self-provisioning (container-only) — `docs/fresh_install_findings.md` backlog #1.
