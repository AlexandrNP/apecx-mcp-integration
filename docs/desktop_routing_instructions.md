# Desktop routing instructions — make the client LLM reach for APECx first

**Audience:** anyone running apecx-mcp behind an MCP client (Claude Desktop, an IDE
extension, a custom agent). **Status:** operational guidance, not code. Verified-needed
(see §4).

## 1. The problem this solves

A connected client LLM, asked a domain question ("RVFV Gn epitopes", "conserved sites in
CHIKV E1"), **defaults to `web_search` or answers from its own knowledge** and never calls
the APECx tools — even though `viral_epitope_evidence_review`, `viral_conserved_sites`, and
`apecx_capabilities` are sitting in its tool list. Observed on a real Claude Desktop session
(2026-06-15) with the apecx MCP server confirmed connected.

Why it happens (and why you can't fix it in this repo):

- A tool's **name + description is only read once the model has decided to use a tool and
  is choosing which one.** For a literature-shaped question the model's prior fires *before*
  that scan — it picks web search / direct answer and never ranks the apecx tools at all. A
  perfectly-worded description cannot win a comparison the model never runs.
- An MCP server's `instructions=` field (we set one in `mcp_surface/server.py`) is
  **advisory** — the client decides whether to inject it into the per-turn system prompt,
  and it loses to the web-search prior here. The server **cannot force** the client to route.

The decisive lever lives **in the client**, in the place the model reads every turn: its
Custom Instructions / Project system prompt. Passive memory ("this user works with APECx")
is not an imperative — the model needs an explicit "check first" rule.

## 2. The routing rule (paste this)

Paste verbatim into **Claude Desktop → Settings → Profile → Custom Instructions** (applies
everywhere), or into a **Project's instructions** (applies in that project only). Use a
Project if you also use Claude Desktop for non-bio work and don't want every chat checking
APECx.

```
APECx tool-routing rule (highest priority — overrides default search behavior):
Before answering ANY question about viruses, viral proteins, epitopes, antigens,
vaccines, pathogens, conserved sites, sequence conservation, or protein structures —
and BEFORE using web_search or answering from memory — you MUST first call
`apecx_capabilities` (or `list_workflows`) to see available APECx workflows, then run
the best match (e.g. `viral_epitope_evidence_review`, `viral_conserved_sites`).
APECx returns grounded, cited evidence from curated databases (BV-BRC, VIOLIN,
PDB/EMDB, PubMed). This is a specialized-data problem, not a web-search problem.
Only fall back to web_search if APECx has no relevant workflow or returns nothing.
```

## 3. Why this is in the client, not the server

| Lever | Where | Effect on routing |
|---|---|---|
| Tool name + description | server (this repo) | Read only *during* tool selection; doesn't trigger selection. Already tuned. |
| Server `instructions=` | server (this repo) | Advisory; client may not surface it per turn. Already set; insufficient alone. |
| **Custom / Project instructions** | **client** | **Read every turn, before the model commits to web search. This is the trigger.** |

We have already done everything useful on the server side (intent-led tool descriptions +
a server `instructions` string, commit `91fbe1f`). Further server wording is a cosmetic
retry of an approach the field test already falsified — don't.

## 4. Verify it worked (do this before declaring it fixed)

In a fresh Claude Desktop chat *with the rule installed and the apecx server connected*,
ask a cold domain question with **no mention of APECx**, e.g.:

> "What are the conserved, structurally accessible epitopes on RVFV Gn?"

**PASS:** the model's first tool call is `apecx_capabilities` (or `list_workflows`),
followed by `run_workflow` on a matching workflow — *not* `web_search`.

**FAIL:** the model calls `web_search` first or answers from memory. If it fails even
with the rule installed, strengthen the rule's first line (make the MUST + "before
web_search" more emphatic) — the routing prior is strong and may need a blunter phrasing
per client/model version. This is the one knob worth turning; server-side wording is not.
