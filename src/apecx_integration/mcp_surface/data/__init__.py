"""Vendored DB-query layer for the integration MCP server.

Source / drift policy
---------------------
The pure-pandas query functions in ``database.py`` are vendored
from ``apecx-mcp/src/apecx_mcp/database.py`` (the standalone data
MCP server in the sibling repo). Vendoring is the chosen
integration shape because the integration repo's runtime deps are
deliberately bounded to ``nanobrain`` and ``apecx-harvesters``
(see ``pyproject.toml`` dep block; user directive 2026-04-27).

If you find a bug in the vendored copy, fix it here AND open a
follow-up to backport / forward-port to ``apecx-mcp`` so the two
copies don't drift indefinitely. The functions are intentionally
small and read-only (no LLM, no I/O beyond CSV-load), which keeps
drift cost low.
"""
