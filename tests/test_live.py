"""Opt-in: uv run pytest -m live. Makes two small real Haiku calls (well under a cent)."""

import os

import anthropic
import pytest

from pai.agent import load_system_prompt
from pai.tools import default_tools

pytestmark = pytest.mark.live


@pytest.fixture
def client():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        from pai.config import Secrets

        os.environ["ANTHROPIC_API_KEY"] = Secrets().anthropic_api_key.get_secret_value()
    return anthropic.Anthropic()


def test_key_works_and_prefix_caches(client):
    system = [
        {"type": "text", "text": load_system_prompt(), "cache_control": {"type": "ephemeral"}}
    ]
    tools = [t.definition() for t in default_tools()]
    usages = []
    for _ in range(2):
        r = client.messages.create(
            model="claude-haiku-5-5",
            max_tokens=64,
            system=system,
            tools=tools,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": "Say hoo."}],
        )
        usages.append(r.usage)
    assert r.stop_reason in ("end_turn", "max_tokens")
    # Only meaningful once the prefix passes the model's minimum cacheable length.
    print("usage:", usages)
    if usages[0].cache_creation_input_tokens:
        assert usages[1].cache_read_input_tokens > 0
