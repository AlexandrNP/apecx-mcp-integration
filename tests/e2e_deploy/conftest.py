"""E2E deployment harness — boots the REAL apecx-mcp tool surface and drives real tool calls + real
workflow runs against real backends. No framework mocks.

This suite is the EXECUTABLE form of ``docs/deployment_and_e2e_verification.md``: it EXERCISES the tool
surface + workflow runs rather than describing them, so a doc-vs-reality drift (wrong tool name, wrong
param, a workflow that no longer completes) fails a test instead of silently misleading an operator.
That drift is exactly what shipped in the prose runbook (harmonized_search documented with `query`
when it needs `term`+`index`); every tool call here uses the SIGNATURE THE CODE ACTUALLY EXPOSES.

Each check is gated to auto-skip when its deployment dependency (docker / ollama / synonym dict) is
absent, so the module stays green on a bare box and does real work when the deployment is provisioned —
the same convention as the existing ``*_against_ollama`` / docker-gated integration suites.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

import pytest

# --------------------------------------------------------------------------- gates


def _docker_up() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _ollama_base() -> str:
    # Normalize an OpenAI-style base (…/v1) down to the ollama host root.
    base = os.environ.get("APECX_LLM_BASE_URL", "http://localhost:11434").rstrip("/")
    return base[:-3].rstrip("/") if base.endswith("/v1") else base


def _ollama_models() -> list[str]:
    try:
        with urllib.request.urlopen(_ollama_base() + "/api/tags", timeout=5) as r:
            if r.status != 200:
                return []
            return [m["name"] for m in json.loads(r.read()).get("models", [])]
    except Exception:  # noqa: BLE001
        return []


def _dict_path() -> Path:
    p = os.environ.get("APECX_SYNONYM_DICT_PATH")
    return Path(p) if p else Path.home() / ".apecx" / "dictionary" / "dictionary.sqlite"


requires_docker = pytest.mark.skipif(not _docker_up(), reason="docker daemon not available")
requires_ollama = pytest.mark.skipif(not _ollama_models(), reason="ollama not reachable / no model")
requires_dict = pytest.mark.skipif(
    not _dict_path().is_file(), reason="synonym dictionary absent (run apecx-setup dict)"
)


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="session")
def deploy_env():
    """Point the process at the REAL deployment resources (dict + ollama) and isolate side-effect dirs
    (design-approval store) into a temp so the harness never pollutes ~/.apecx or ~/.cache/apecx.

    The LLM model is resolved to whatever the running ollama actually serves (portable across hosts)
    unless the operator pins APECX_LLM_MODEL. This mirrors a provisioned deployment, not a fresh
    bootstrap — the fresh dict download + backend bring-up are exercised separately (Phase C)."""
    tmp = Path(tempfile.mkdtemp(prefix="apecx_e2e_deploy_"))
    overrides = {
        "APECX_SYNONYM_DICT_PATH": str(_dict_path()),
        "APECX_DESIGN_APPROVAL_DIR": str(tmp / "approvals"),
        "APECX_LLM_BASE_URL": _ollama_base(),
    }
    if "APECX_LLM_MODEL" not in os.environ:
        models = _ollama_models()
        if models:
            overrides["APECX_LLM_MODEL"] = models[0]
    prev = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    yield tmp
    for k, v in prev.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture(scope="session")
def server(deploy_env):
    """The REAL FastMCP server, built exactly as `apecx-mcp` builds it (minus stdio)."""
    from apecx_integration.mcp_surface.server import build_server

    return build_server()


@pytest.fixture
def call(server):
    """A tool caller bound to the real server: ``call(name, args) -> payload dict``."""

    def _call(name: str, args: dict):
        return call_tool(server, name, args)

    return _call


@pytest.fixture
def ollama_or_skip():
    if not _ollama_models():
        pytest.skip("ollama not reachable / no model (a workflow run needs a live LLM)")


@pytest.fixture
def dict_or_skip():
    if not _dict_path().is_file():
        pytest.skip("synonym dictionary absent (run apecx-setup dict)")


# --------------------------------------------------------------------------- tool-call helper


def call_tool(server, name: str, args: dict):
    """Invoke a tool through the REAL FastMCP dispatch and return its JSON payload as a dict/list.

    FastMCP's `call_tool` returns either a list of content blocks or a `(content, structured)` tuple;
    this normalizes both to the tool's actual JSON return value — what a real MCP client would parse.
    """
    res = asyncio.run(server.call_tool(name, args))
    return _payload(res)


def _payload(res):
    if isinstance(res, tuple):
        for part in res:  # a structured-result dict is the tool's real return value
            if isinstance(part, dict):
                return part
        res = res[0]
    if isinstance(res, dict):
        return res
    for block in res or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except Exception:  # noqa: BLE001
                return {"_text": text}
    return {"_raw": repr(res)}
