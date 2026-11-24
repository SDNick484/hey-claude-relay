import json

import anyio
import httpx2
import pytest
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient

from fakes import FakeClaude, refuse, say, use
from hey_claude_relay.claude import AnthropicClaude, text_of
from hey_claude_relay.config import ConfigError, RelayConfig, Secrets
from hey_claude_relay.errors import RelayError

TOOL = {"name": "matinee__where_is", "description": "Find a title", "input_schema": {"type": "object"}}
MSGS = [{"role": "user", "content": "hi"}]
REPLY = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-5-5",
    "content": [{"type": "text", "text": "Hello."}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 3, "output_tokens": 2},
}


class MockAnthropic:
    """The real SDK client with only the network replaced, so the request body is what would be sent."""

    def __init__(self) -> None:
        self.bodies: list[dict] = []
        self.headers: list[dict] = []
        self._status = 200
        self._hang = False
        http = DefaultAsyncHttpxClient(transport=httpx2.MockTransport(self._handle))
        self.client = AsyncAnthropic(api_key="sk-test-key", http_client=http, max_retries=0)

    async def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.bodies.append(json.loads(request.content))
        self.headers.append(dict(request.headers))
        if self._hang:
            await anyio.sleep(30)
        if self._status != 200:
            error = {"type": "error", "error": {"type": "api_error", "message": "secret detail from upstream"}}
            return httpx2.Response(self._status, json=error, headers={"request-id": "req_123"})
        return httpx2.Response(200, json=REPLY)

    def respond(self, status: int) -> None:
        self._status = status

    def hang(self) -> None:
        self._hang = True


@pytest.fixture
def mock_anthropic():
    return MockAnthropic()


async def create(mock, cfg=None, **overrides):
    kwargs = {"system": "S", "tools": [TOOL], "messages": MSGS, "allow_tools": True, "timeout": 5} | overrides
    return await AnthropicClaude(cfg or RelayConfig(), mock.client).create(**kwargs)


async def test_request_shape(mock_anthropic):
    reply = await create(mock_anthropic)
    body = mock_anthropic.bodies[0]
    assert body["model"] == "claude-sonnet-5-5" and body["max_tokens"] == 16000
    assert body["system"] == "S" and body["tools"] == [TOOL] and body["messages"] == MSGS
    assert body["output_config"] == {"effort": "low"} and body["cache_control"] == {"type": "ephemeral"}
    assert "thinking" not in body and "tool_choice" not in body and "temperature" not in body
    assert text_of(reply) == "Hello."


async def test_model_effort_and_max_tokens_come_from_config(mock_anthropic):
    await create(mock_anthropic, RelayConfig(model="claude-haiku-5-5", effort="medium", max_tokens=4096))
    body = mock_anthropic.bodies[0]
    assert (body["model"], body["output_config"], body["max_tokens"]) == (
        "claude-haiku-5-5",
        {"effort": "medium"},
        4096,
    )


async def test_wrap_up_call_forbids_tools(mock_anthropic):
    await create(mock_anthropic, allow_tools=False)
    assert mock_anthropic.bodies[0]["tool_choice"] == {"type": "none"}


async def test_no_effort_key_when_config_has_none(mock_anthropic):
    await create(mock_anthropic, RelayConfig(effort=None), tools=[])
    assert "output_config" not in mock_anthropic.bodies[0]


async def test_no_tools_key_when_there_are_no_tools(mock_anthropic):
    await create(mock_anthropic, tools=[])
    assert "tools" not in mock_anthropic.bodies[0] and "tool_choice" not in mock_anthropic.bodies[0]


@pytest.mark.parametrize(
    "status, code",
    [
        (429, "rate_limited"),
        (500, "upstream_error"),
        (529, "upstream_error"),
        (401, "upstream_error"),
        (400, "upstream_error"),
    ],
)
async def test_api_errors_become_spoken_relay_errors(mock_anthropic, status, code, caplog):
    mock_anthropic.respond(status)
    with pytest.raises(RelayError) as e:
        await create(mock_anthropic)
    assert e.value.code == code and e.value.spoken
    assert "secret detail from upstream" not in e.value.spoken
    assert f"status={status}" in caplog.text and "req_123" in caplog.text
    assert "secret detail from upstream" not in caplog.text and "sk-test-key" not in caplog.text


async def test_timeout_is_upstream_timeout(mock_anthropic):
    mock_anthropic.hang()
    with pytest.raises(RelayError) as e:
        await create(mock_anthropic, timeout=0.05)
    assert e.value.code == "upstream_timeout" and e.value.spoken


async def test_a_dead_connection_is_upstream_error():
    def refuse_connection(request):
        raise httpx2.ConnectError("no route to host")

    http = DefaultAsyncHttpxClient(transport=httpx2.MockTransport(refuse_connection))
    claude = AnthropicClaude(RelayConfig(), AsyncAnthropic(api_key="sk-test-key", http_client=http, max_retries=0))
    with pytest.raises(RelayError) as e:
        await claude.create(system="S", tools=[], messages=MSGS, allow_tools=True, timeout=5)
    assert e.value.code == "upstream_error"


async def test_a_deadline_in_the_caller_is_not_reported_as_a_claude_timeout(mock_anthropic):
    # The turn's own time limit cancels the call from outside; that must stay a cancellation.
    mock_anthropic.hang()
    with anyio.move_on_after(0.05) as scope:
        await create(mock_anthropic, timeout=5)
    assert scope.cancelled_caught


def test_from_secrets_needs_the_api_key():
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        AnthropicClaude.from_secrets(RelayConfig(), Secrets())
    claude = AnthropicClaude.from_secrets(RelayConfig(), Secrets(anthropic_api_key="sk-test-key"))
    assert claude.client.max_retries == 1  # one retry on a 5xx or 429 fits inside a turn; more would not


def test_text_of_reads_by_block_type():
    assert text_of(use(("a__b", {}), text="Looking.")) == "Looking."
    assert text_of(say("One.")) == "One." and text_of(say("")) == "" and text_of(refuse()) == ""


async def test_fake_claude_follows_its_script_and_records_copies():
    messages = [{"role": "user", "content": "hi"}]
    fake = FakeClaude(say("One."), RelayError("upstream_error", "x"), lambda call: say(call["messages"][0]["content"]))
    assert text_of(await fake.create(system="S", tools=[], messages=messages, allow_tools=True, timeout=1)) == "One."
    messages[0]["content"] = "changed later"
    assert fake.calls[0]["messages"] == [{"role": "user", "content": "hi"}]
    with pytest.raises(RelayError):
        await fake.create(system="S", tools=[], messages=messages, allow_tools=True, timeout=1)
    assert (
        text_of(await fake.create(system="S", tools=[], messages=messages, allow_tools=True, timeout=1))
        == "changed later"
    )
    with pytest.raises(AssertionError, match="script"):
        await fake.create(system="S", tools=[], messages=messages, allow_tools=True, timeout=1)
    assert "changed later" in fake.sent()
