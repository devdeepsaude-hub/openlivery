"""Tool-calling loop over the chat completions dialect.

Mirrors the request building in services/ai.py but keeps requesting until the
model stops asking for tools. Mid-loop responses may contain no text, so this
module never routes them through the plain-completion text extractor until
the loop ends.
"""

import json

from ...config import get_settings
from ..anthropic_native import NATIVE_CONTENT
from ..ai import Completion, Usage, _post_json, chat_message, completion_from, extract_chat_text, read_usage, sampling_params, send_chat
from ..tool_files import MAX_TOOL_FILES
from .http_exec import execute_http_tool
from .mcp_client import call_mcp_tool
from .specs import ToolSpec, find_spec

MAX_TOOL_ITERATIONS = 5
RESULT_PREVIEW_CHARS = 500


async def _execute(spec: ToolSpec, args: dict) -> tuple[str, bool, list]:
    files: list = []
    if spec.handler is not None:
        result, is_error = spec.handler(args)
    elif spec.mcp_tool_name is not None:
        result, is_error = await call_mcp_tool(spec.tool, spec.mcp_tool_name, args)
    else:
        result, is_error, files = await execute_http_tool(spec.tool, args)
    if is_error:
        # Unambiguous failure marker (the tool message has no error flag) so
        # the no-fallback rule in the system prompt kicks in.
        result = f"Tool call failed: {result}"
    return result, is_error, files


def _record(metadata: list[dict], name: str, args: dict, result: str, is_error: bool) -> None:
    # Kept at least as long as the history note carries.
    limit = max(RESULT_PREVIEW_CHARS, get_settings().tool_note_result_chars)
    preview = result[:limit] + ("…" if len(result) > limit else "")
    metadata.append({"name": name, "arguments": args, "result_preview": preview, "is_error": is_error})


async def tool_loop(
    base_url: str, api_key: str, model: str, messages: list[dict], specs: list[ToolSpec],
    temperature: float | None, max_tokens: int | None,
) -> Completion:
    convo: list[dict] = [{"role": m["role"], "content": m["content"]} for m in messages]
    # strict is stated explicitly: some routes treat an omitted value as
    # strict mode, where the model must emit every property in the schema
    # and fills the optional ones with empty values.
    tools = [
        {"type": "function", "function": {"name": s.name, "description": s.description, "parameters": s.input_schema, "strict": False}}
        for s in specs
    ]
    sampling = sampling_params(temperature, max_tokens)
    usage = Usage()
    metadata: list[dict] = []
    attachments: list = []

    for iteration in range(MAX_TOOL_ITERATIONS + 1):
        # usage.include asks OpenRouter to price each turn in the response; the
        # per-turn costs add up across the loop in ``read_usage``.
        payload: dict = {"model": model, "messages": convo, "tools": tools, "usage": {"include": True}}
        if iteration == MAX_TOOL_ITERATIONS:
            # Cap reached: tools stay in the payload (required when the history
            # contains tool calls) but the model must answer with text.
            payload["tool_choice"] = "none"
        data = await send_chat(base_url, api_key, payload, sampling, post=_post_json)
        usage = usage + read_usage(data)
        message = chat_message(data)
        calls = [call for call in (message.get("tool_calls") or []) if call.get("type", "function") == "function"]
        if not calls:
            completion = completion_from(extract_chat_text(data), usage, data, tool_calls=metadata or None)
            completion.attachments = attachments
            return completion
        # The assistant turn that asked for the tools must be echoed back, then
        # one tool message per call, in the same order.
        echoed = {"role": "assistant", "content": message.get("content") or "", "tool_calls": calls}
        if message.get(NATIVE_CONTENT):
            # Anthropic's own content blocks (thinking included), which its API
            # needs back to continue the round; see anthropic_native.
            echoed[NATIVE_CONTENT] = message[NATIVE_CONTENT]
        convo.append(echoed)
        for call in calls:
            function = call.get("function") or {}
            try:
                args = json.loads(function.get("arguments") or "{}")
            except ValueError:
                args = {}
            spec = find_spec(specs, function.get("name", ""))
            if spec is None:
                result, is_error, files = f"Error: unknown tool '{function.get('name')}'", True, []
            else:
                result, is_error, files = await _execute(spec, args)
            if files:
                attachments.extend(files)
                del attachments[MAX_TOOL_FILES:]
            _record(metadata, function.get("name", ""), args, result, is_error)
            convo.append({"role": "tool", "tool_call_id": call.get("id"), "content": result})
    raise ValueError("tool loop did not converge")
