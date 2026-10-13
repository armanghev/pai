from types import SimpleNamespace

import pytest

from pai import db
from pai.config import Config
from pai.tools import ToolContext


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "pai.db")
    yield c
    c.close()


@pytest.fixture
def config(tmp_path):
    return Config(
        timezone="America/Los_Angeles",
        db_path=tmp_path / "pai.db",
        notes_path=tmp_path / "notes.md",
    )


@pytest.fixture
def sent():
    return []


@pytest.fixture
def ctx(conn, config, sent):
    async def send(text):
        sent.append(text)

    return ToolContext(conn=conn, config=config, send=send)


def usage(inp=100, out=20, read=0, write=0):
    return SimpleNamespace(
        input_tokens=inp,
        output_tokens=out,
        cache_read_input_tokens=read,
        cache_creation_input_tokens=write,
    )


def text_block(text):
    return SimpleNamespace(type="text", text=text)


def tool_block(name, input=None, id="tu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=input or {}, id=id)


def response(content, stop_reason="end_turn", model="claude-haiku-5-5", **u):
    return SimpleNamespace(content=content, stop_reason=stop_reason, model=model, usage=usage(**u))


class FakeClient:
    """Returns scripted responses and records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)
