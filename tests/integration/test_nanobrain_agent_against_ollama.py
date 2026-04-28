"""T02 / memo-07: proof that a nanobrain Agent boots + answers against
a local Ollama daemon using the patched ``AgentConfig``.

Auto-skips when:
- Ollama daemon not reachable at http://localhost:11434, or
- No model is installed.

This test is the load-bearing integration test for scope memo 07. It
exercises three concrete claims:
  1. ``AgentConfig.from_config`` accepts the three new fields
     (``provider``, ``base_url``, ``api_key``).
  2. ``SimpleAgent._initialize_llm_client`` dispatches to the local
     branch when ``provider: openai_compatible`` is set and succeeds
     without an OPENAI_API_KEY in the environment.
  3. The resulting ``AsyncOpenAI`` client can reach Ollama via the
     OpenAI-compatible endpoint and produce a non-empty reply.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import httpx
import pytest
from nanobrain.core.agent import SimpleAgent

pytestmark = pytest.mark.integration

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "mistral-small:latest")


def _skip_live_llm_requested() -> bool:
    """Opt-out env var for Claude-Code sessions: set
    ``APECX_SKIP_LIVE_LLM=1`` to force-skip every live-LLM test in
    this file, regardless of whether the daemon is reachable."""
    return os.environ.get("APECX_SKIP_LIVE_LLM") == "1"


def _ollama_reachable_with_model(model: str) -> bool:
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=2.0)
        r.raise_for_status()
        names = {m["name"] for m in r.json().get("models", [])}
        return model in names
    except Exception:
        return False


SKIP_LIVE_LLM_OPTOUT = "APECX_SKIP_LIVE_LLM=1 is set — live-LLM tests explicitly skipped."
SKIP_REASON = (
    f"Ollama daemon not reachable at {OLLAMA_URL} or model {OLLAMA_MODEL} "
    "not pulled. Run `ollama serve` + `ollama pull mistral-small:latest`."
)


@pytest.fixture
def ollama_agent_yaml(tmp_path) -> Path:
    """Write a minimal SimpleAgent YAML pointed at Ollama and return the
    path. Using a fixture (not a module-level constant) means each test
    gets a throwaway YAML that can vary parameters if needed.
    """
    yaml_path = tmp_path / "agent.yml"
    yaml_path.write_text(
        textwrap.dedent(
            f"""\
            name: ollama_smoke_agent
            description: "Smoke-test agent proving memo-07 local-LLM patch works."
            provider: openai_compatible
            base_url: "{OLLAMA_URL}/v1"
            model: "{OLLAMA_MODEL}"
            system_prompt: "You reply with a single word. No punctuation."
            temperature: 0.0
            max_tokens: 8
            auto_initialize: true
            enable_logging: false
            log_conversations: false
            log_tool_calls: false
            """
        )
    )
    return yaml_path


@pytest.mark.skipif(_skip_live_llm_requested(), reason=SKIP_LIVE_LLM_OPTOUT)
@pytest.mark.skipif(not _ollama_reachable_with_model(OLLAMA_MODEL), reason=SKIP_REASON)
async def test_simple_agent_initializes_against_ollama(
    ollama_agent_yaml: Path, monkeypatch
) -> None:
    """Agent boots cleanly without OPENAI_API_KEY — proves the memo-07
    patch relaxes the API-key gate for local endpoints.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    agent = SimpleAgent.from_config(str(ollama_agent_yaml))
    await agent.initialize()

    assert agent.llm_client is not None, (
        "Agent failed to initialize LLM client against Ollama. "
        "Check /tmp/claude-*/ tasks or the nanobrain agent logs for the "
        "_initialize_llm_client error."
    )
    assert agent.config.provider == "openai_compatible"
    assert agent.config.base_url == f"{OLLAMA_URL}/v1"


@pytest.mark.skipif(_skip_live_llm_requested(), reason=SKIP_LIVE_LLM_OPTOUT)
@pytest.mark.skipif(not _ollama_reachable_with_model(OLLAMA_MODEL), reason=SKIP_REASON)
async def test_simple_agent_process_returns_nonempty_reply(
    ollama_agent_yaml: Path, monkeypatch
) -> None:
    """End-to-end: Agent.process() round-trips through Ollama and
    returns a non-empty reply. This is the load-bearing claim — without
    it, memo-07 is just config plumbing and doesn't prove anything.
    """
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    agent = SimpleAgent.from_config(str(ollama_agent_yaml))
    await agent.initialize()

    reply = await agent.process("Say the word: ready")
    assert isinstance(reply, str), f"Expected str, got {type(reply).__name__}"
    assert reply.strip(), "Empty reply from Ollama — model may be misconfigured."
