"""Claude through Anthropic's Messages API, with prompt caching."""

import asyncio
import copy
import json
from types import SimpleNamespace

from app.config import get_settings
from app.services import ai, anthropic_native
from app.services.anthropic_native import NATIVE_CONTENT, to_chat_response, to_request
from app.services.prompt_cache import CACHE_BOUNDARY, insert_stable, without_boundary

FIXED = "# Mariana\n\nJob, rules, tone and FAQ."
TOOLS = [
    {"type": "function", "function": {"name": "reservar_horario", "description": "Books.", "parameters": {"type": "object", "properties": {"hora": {"type": "string"}}}, "strict": False}},
    {"type": "function", "function": {"name": "consultar_horarios", "description": "Looks up.", "parameters": {"type": "object", "properties": {}}}},
]


def _system(now: str) -> dict:
    return {"role": "system", "content": f"{FIXED}{CACHE_BOUNDARY}Data e hora atuais: {now}.\n\nRESERVAS: nenhuma."}


def _strip_marks(value):
    """The request without cache markers: they move between requests and are no part of the prefix."""
    if isinstance(value, dict):
        return {key: _strip_marks(item) for key, item in value.items() if key != "cache_control"}
    if isinstance(value, list):
        return [_strip_marks(item) for item in value]
    return value


def test_the_system_prompt_is_split_and_the_changing_part_rides_in_the_newest_turn():
    payload = {"model": "claude-sonnet-5-5", "tools": TOOLS, "messages": [
        _system("sábado 14:03"),
        {"role": "user", "content": "Oi"},
        {"role": "assistant", "content": "Oi! Sou a Mariana."},
        {"role": "user", "content": "Quero marcar"},
        {"role": "user", "content": "à noite"},
    ]}
    request = to_request(payload, {"max_tokens": 4000, "thinking": {"type": "adaptive"}})
    assert request["system"] == [{"type": "text", "text": FIXED, "cache_control": {"type": "ephemeral"}}]
    assert request["cache_control"] == {"type": "ephemeral"}
    assert request["thinking"] == {"type": "adaptive"} and request["max_tokens"] == 4000
    # Two user messages in a row are one turn; the changing part opens it.
    newest = request["messages"][-1]
    assert newest["role"] == "user"
    assert newest["content"][0]["text"].startswith("<system_context>\nData e hora atuais: sábado 14:03.")
    assert [block["text"] for block in newest["content"][1:]] == ["Quero marcar", "à noite"]
    # The previous answer carries the breakpoint for the history.
    assert request["messages"][-2]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    # Tools sorted by name, strict dropped, schema kept.
    assert [tool["name"] for tool in request["tools"]] == ["consultar_horarios", "reservar_horario"]
    assert request["tools"][1]["input_schema"]["properties"] == {"hora": {"type": "string"}}


def test_consecutive_turns_share_their_prefix_byte_for_byte():
    history = [
        {"role": "user", "content": "Oi"},
        {"role": "assistant", "content": "Oi! Sou a Mariana. A terapia seria pra você?"},
        {"role": "user", "content": "Sim, pra mim"},
    ]
    turn_one = to_request({"model": "m", "tools": TOOLS, "messages": [_system("14:03"), *history]}, {})
    later = [*history, {"role": "assistant", "content": "Já fez terapia antes?"}, {"role": "user", "content": "Nunca"}]
    turn_two = to_request({"model": "m", "tools": TOOLS, "messages": [_system("14:09"), *later]}, {})
    # The breakpoint turn one wrote (on the reply before its newest turn) is a
    # prefix of turn two, though the time in the prompt changed.
    written = len(turn_one["messages"]) - 1
    assert _strip_marks(turn_two["messages"][:written]) == _strip_marks(turn_one["messages"][:written])
    assert turn_two["system"] == turn_one["system"] and turn_two["tools"] == turn_one["tools"]


def test_tool_rounds_keep_the_prefix_and_echo_anthropic_blocks():
    first = {"model": "m", "tools": TOOLS, "messages": [_system("14:03"), {"role": "user", "content": "Tem horário à noite?"}]}
    round_one = to_request(copy.deepcopy(first), {})
    echoed = {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "toolu_1", "type": "function", "function": {"name": "consultar_horarios", "arguments": "{}"}}],
        NATIVE_CONTENT: [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "tool_use", "id": "toolu_1", "name": "consultar_horarios", "input": {}},
        ],
    }
    second = {**first, "messages": [*first["messages"], echoed, {"role": "tool", "tool_call_id": "toolu_1", "content": "Tool call failed: 500"}]}
    round_two = to_request(second, {})
    assert _strip_marks(round_two["messages"][0]) == _strip_marks(round_one["messages"][0])
    assistant, results = round_two["messages"][1], round_two["messages"][2]
    assert assistant["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig"}
    assert results["content"] == [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "Tool call failed: 500", "is_error": True}]


def test_history_may_not_open_with_the_business():
    request = to_request({"model": "m", "messages": [{"role": "assistant", "content": "Oi!"}, {"role": "user", "content": "Oi"}]}, {})
    assert request["messages"][0] == {"role": "user", "content": [{"type": "text", "text": "[...]"}]}
    assert "system" not in request and "tools" not in request


def test_plain_tool_calls_become_tool_use_blocks_and_choices_map():
    payload = {"model": "m", "tools": TOOLS, "tool_choice": "none", "messages": [
        {"role": "user", "content": "Oi"},
        {"role": "assistant", "content": "Vou ver.", "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "consultar_horarios", "arguments": "{\"turno\": \"noite\"}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "[]"},
    ]}
    request = to_request(payload, {}, ttl="1h")
    assert request["tool_choice"] == {"type": "none"} and request["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert request["messages"][1]["content"] == [
        {"type": "text", "text": "Vou ver."},
        {"type": "tool_use", "id": "t1", "name": "consultar_horarios", "input": {"turno": "noite"}},
    ]


def _message(content, stop="end_turn", read=0, written=0):
    usage = SimpleNamespace(input_tokens=120, output_tokens=40, cache_read_input_tokens=read, cache_creation_input_tokens=written)
    return SimpleNamespace(id="msg_1", model="claude-sonnet-5-5", content=content, stop_reason=stop, usage=usage)


def test_the_answer_comes_back_in_the_chat_completions_shape():
    data = to_chat_response(_message([
        SimpleNamespace(type="thinking", thinking="", signature="sig"),
        SimpleNamespace(type="text", text="Um momento."),
        SimpleNamespace(type="tool_use", id="toolu_9", name="consultar_horarios", input={"turno": "noite"}),
    ], stop="tool_use", read=9000, written=500))
    message = data["choices"][0]["message"]
    assert message["content"] == "Um momento." and data["choices"][0]["finish_reason"] == "tool_calls"
    assert message["tool_calls"][0]["function"] == {"name": "consultar_horarios", "arguments": json.dumps({"turno": "noite"})}
    assert message[NATIVE_CONTENT][0] == {"type": "thinking", "thinking": "", "signature": "sig"}
    usage = ai.read_usage(data)
    assert usage.input_tokens == 9620 and usage.cached_tokens == 9000 and usage.output_tokens == 40


def test_send_chat_goes_native_for_anthropic_when_enabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "anthropic_native", True)
    sent = {}

    async def create(api_key, request, timeout):
        sent.update(request)
        return _message([SimpleNamespace(type="text", text="Oi!")], read=800)

    async def unwired(*args, **kwargs):
        raise AssertionError("the OpenAI-compatible endpoint must not be called")

    async def wire_model(base_url, api_key, model):
        return model.split("/", 1)[-1]

    monkeypatch.setattr(anthropic_native, "create_message", create)
    monkeypatch.setattr(ai, "_post_json", unwired)
    monkeypatch.setattr(ai.vendors, "wire_model", wire_model)
    payload = {"model": "anthropic/claude-sonnet-5-5", "messages": [_system("14:03"), {"role": "user", "content": "Oi"}]}
    data = asyncio.run(ai.send_chat("https://api.anthropic.com/v1", "key", payload, {"max_tokens": 900}))
    assert ai.extract_chat_text(data) == "Oi!" and ai.read_usage(data).cached_tokens == 800
    assert sent["model"] == "claude-sonnet-5-5" and sent["system"][0]["text"] == FIXED
    assert sent["messages"][-1]["content"][0]["text"].startswith("<system_context>")


def test_every_other_request_gets_the_plain_prompt(monkeypatch):
    monkeypatch.setattr(get_settings(), "anthropic_native", False)
    seen = {}

    async def post(url, headers, payload, sampling, timeout=90):
        seen.update(payload)
        return {"choices": [{"message": {"role": "assistant", "content": "Oi!"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    monkeypatch.setattr(ai, "_post_json", post)
    payload = {"model": "deepseek/deepseek-v4-pro", "messages": [
        _system("14:03"), {"role": "assistant", "content": "", "tool_calls": [], NATIVE_CONTENT: [{"type": "thinking"}]},
    ]}
    asyncio.run(ai.send_chat("https://openrouter.ai/api/v1", "key", payload, {}))
    assert CACHE_BOUNDARY not in seen["messages"][0]["content"] and FIXED in seen["messages"][0]["content"]
    assert NATIVE_CONTENT not in seen["messages"][1]


def test_boundary_helpers():
    system = f"fixed{CACHE_BOUNDARY}changing"
    assert insert_stable(system, "rules") == f"fixed\n\nrules{CACHE_BOUNDARY}changing"
    assert insert_stable("no marker", "rules") == "no marker\n\nrules"
    assert without_boundary(system) == "fixed\n\nchanging"


def test_the_fixed_part_of_a_real_prompt_does_not_change_with_the_time(authenticated_client, monkeypatch):
    from datetime import datetime as real_datetime

    from conftest import TestingSession

    from app.models import Agent
    from app.services import knowledge

    client = authenticated_client
    customer = client.post("/api/clients", json={"name": "Clinica Aurora", "is_active": True}).json()
    agent = client.post("/api/agents", json={
        "client_id": customer["id"], "provider": "openrouter", "model": "anthropic/claude-sonnet-5-5", "name": "Mariana",
        "prompt_language": "pt", "instructions": "Acolha quem procura terapia.", "brief_products": "Terapia online.",
    }).json()

    def prompt_at(hour: int) -> str:
        class Frozen(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return real_datetime(2026, 10, 25, hour, 7, tzinfo=tz)

        monkeypatch.setattr(knowledge, "datetime", Frozen)
        with TestingSession() as db:
            return knowledge.build_system_prompt(db.get(Agent, agent["id"]), "", "Falta pedir o e-mail.")

    morning, night = prompt_at(9), prompt_at(21)
    assert morning != night
    assert morning.split(CACHE_BOUNDARY)[0] == night.split(CACHE_BOUNDARY)[0]
    # What changes lives after the boundary.
    assert "09:07" in morning.split(CACHE_BOUNDARY)[1] and "Falta pedir o e-mail." in morning.split(CACHE_BOUNDARY)[1]
