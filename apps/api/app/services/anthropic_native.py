"""Claude through Anthropic's own Messages API, with prompt caching.

Every vendor is spoken to in the chat-completions dialect, and Claude is
reached through Anthropic's OpenAI-compatible endpoint, which does not cache
prompts: each reply pays full price for the whole system prompt and history.
With ``ANTHROPIC_NATIVE`` on, requests to Anthropic go to the Messages API
through the official SDK instead, translated from and back to the
chat-completions shape the rest of the code uses, with cache breakpoints
placed so the shared prefix stays byte-identical from request to request:

- the system prompt is split at ``prompt_cache.CACHE_BOUNDARY``: the part
  before it (the agent's fixed prompt, the same for all its conversations)
  carries a breakpoint; the part after it (the time, the contact's bookings,
  per-turn notes) moves to the top of the newest user turn, so a change there
  never invalidates the cached history;
- the turn before the newest user turn carries a breakpoint, so each reply
  reads the conversation up to the previous answer from cache;
- automatic caching covers the tool rounds inside one reply.

Tools go sorted by name (a different order is a different prefix). The
assistant turn that asked for tools is echoed back with its thinking blocks,
which the API needs to continue a tool round; the tool loop carries them
under ``NATIVE_CONTENT``, and every other vendor gets that key stripped.
"""

import json

import httpx

from ..config import get_settings
from .prompt_cache import CACHE_BOUNDARY
from .vendors import ProviderError

NATIVE_CONTENT = "_anthropic_content"

# The per-turn part of the system prompt, at the top of the newest user turn.
_CONTEXT = "<system_context>\n{}\n</system_context>"
# A conversation must open with a user turn; history cut by the memory limit
# can open with the business speaking.
_OPENING = "[...]"
_CACHEABLE = {"text", "image", "tool_use", "tool_result", "document"}
_FINISH = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "tool_use": "tool_calls", "refusal": "content_filter"}


def _cache(ttl: str) -> dict:
    return {"type": "ephemeral", "ttl": ttl} if ttl and ttl != "5m" else {"type": "ephemeral"}


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(part.get("text", "") for part in content or [] if isinstance(part, dict) and part.get("type") == "text")


def _blocks(content) -> list[dict]:
    """Chat-completions message content as Messages API content blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content.strip() else []
    blocks = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and (part.get("text") or "").strip():
            blocks.append({"type": "text", "text": part["text"]})
        elif part.get("type") == "image_url":
            url = (part.get("image_url") or {}).get("url") or ""
            if url.startswith("data:") and "," in url:
                header, data = url.split(",", 1)
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": header[5:].split(";")[0], "data": data}})
            elif url:
                blocks.append({"type": "image", "source": {"type": "url", "url": url}})
    return blocks


def _append(turns: list[dict], role: str, blocks: list[dict]) -> None:
    if not blocks:
        return
    if turns and turns[-1]["role"] == role:
        turns[-1]["content"].extend(blocks)
    else:
        turns.append({"role": role, "content": list(blocks)})


def _assistant_blocks(message: dict) -> list[dict]:
    if message.get(NATIVE_CONTENT):
        return [dict(block) for block in message[NATIVE_CONTENT]]
    blocks = _blocks(message.get("content"))
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except ValueError:
            arguments = {}
        blocks.append({"type": "tool_use", "id": call.get("id"), "name": function.get("name"),
                       "input": arguments if isinstance(arguments, dict) else {}})
    return blocks


def to_request(payload: dict, sampling: dict, ttl: str = "5m") -> dict:
    """A chat-completions request as Messages API parameters."""
    system = []
    turns: list[dict] = []
    for message in payload["messages"]:
        role = message["role"]
        if role in ("system", "developer"):
            system.append(_text(message["content"]))
        elif role == "tool":
            result = str(message.get("content") or "")
            block = {"type": "tool_result", "tool_use_id": message.get("tool_call_id"), "content": result}
            if result.startswith("Tool call failed:"):
                block["is_error"] = True
            _append(turns, "user", [block])
        elif role == "assistant":
            _append(turns, "assistant", _assistant_blocks(message))
        else:
            _append(turns, "user", _blocks(message.get("content")))
    if not turns or turns[0]["role"] != "user":
        turns.insert(0, {"role": "user", "content": [{"type": "text", "text": _OPENING}]})

    fixed, _, changing = "\n\n".join(part for part in system if part).partition(CACHE_BOUNDARY)
    cache = _cache(ttl)
    # The newest turn the person wrote (tool rounds add user turns of results only).
    newest = next((i for i in range(len(turns) - 1, -1, -1) if turns[i]["role"] == "user"
                   and any(block["type"] != "tool_result" for block in turns[i]["content"])), len(turns) - 1)
    if changing.strip():
        content = turns[newest]["content"]
        at = next((i for i, block in enumerate(content) if block["type"] != "tool_result"), len(content))
        content.insert(at, {"type": "text", "text": _CONTEXT.format(changing.strip())})
    if newest > 0:
        previous = turns[newest - 1]["content"]
        for block in reversed(previous):
            if block["type"] in _CACHEABLE:
                block["cache_control"] = cache
                break

    request = {
        "model": payload["model"],
        "max_tokens": int(sampling.get("max_tokens") or 4096),
        "messages": turns,
        "cache_control": cache,
    }
    if fixed.strip():
        request["system"] = [{"type": "text", "text": fixed, "cache_control": cache}]
    tools = sorted(
        ({"name": (tool.get("function") or {}).get("name"),
          "description": (tool.get("function") or {}).get("description") or "",
          "input_schema": (tool.get("function") or {}).get("parameters") or {"type": "object", "properties": {}}}
         for tool in payload.get("tools") or []),
        key=lambda tool: tool["name"] or "",
    )
    if tools:
        request["tools"] = tools
        choice = payload.get("tool_choice")
        if choice == "none":
            request["tool_choice"] = {"type": "none"}
        elif choice == "required":
            request["tool_choice"] = {"type": "any"}
        elif isinstance(choice, dict) and (choice.get("function") or {}).get("name"):
            request["tool_choice"] = {"type": "tool", "name": choice["function"]["name"]}
    if sampling.get("thinking"):
        request["thinking"] = sampling["thinking"]
    return request


def _echo(block) -> dict | None:
    """A response content block as it must be sent back to continue the turn."""
    if block.type == "thinking":
        return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
    if block.type == "redacted_thinking":
        return {"type": "redacted_thinking", "data": block.data}
    if block.type == "text" and block.text:
        return {"type": "text", "text": block.text}
    if block.type == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    return None


def to_chat_response(message) -> dict:
    """A Messages API response in the chat-completions shape."""
    text = "".join(block.text for block in message.content if block.type == "text")
    calls = [
        {"id": block.id, "type": "function", "function": {"name": block.name, "arguments": json.dumps(block.input, ensure_ascii=False)}}
        for block in message.content if block.type == "tool_use"
    ]
    reply: dict = {"role": "assistant", "content": text}
    if calls:
        reply["tool_calls"] = calls
        reply[NATIVE_CONTENT] = [echo for echo in map(_echo, message.content) if echo]
    usage = message.usage
    read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    written = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    return {
        "id": message.id,
        "model": message.model,
        "choices": [{"index": 0, "message": reply, "finish_reason": _FINISH.get(message.stop_reason, "stop")}],
        "usage": {
            "prompt_tokens": int(usage.input_tokens or 0) + read + written,
            "completion_tokens": int(usage.output_tokens or 0),
            "prompt_tokens_details": {"cached_tokens": read, "cache_write_tokens": written},
        },
    }


def _detail(exc) -> str:
    body = getattr(exc, "body", None)
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return f"The AI provider responded with an error: {message or getattr(exc, 'message', '') or type(exc).__name__}"


async def create_message(api_key: str, request: dict, timeout: float):
    """One Messages API call through the official SDK, with its errors turned
    into the ones the chat-completions path raises."""
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=1)
    try:
        return await client.messages.create(**request)
    except anthropic.APIStatusError as exc:
        raise ProviderError(exc.status_code, _detail(exc)) from exc
    except anthropic.APIConnectionError as exc:
        raise httpx.ConnectError(str(exc)) from exc
    finally:
        await client.close()


async def complete(api_key: str, payload: dict, sampling: dict, timeout: float) -> dict:
    """Send a chat-completions request to Claude natively; the answer comes
    back in the chat-completions shape."""
    request = to_request(payload, sampling, get_settings().anthropic_cache_ttl)
    return to_chat_response(await create_message(api_key, request, timeout))
