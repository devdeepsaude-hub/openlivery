"""Direct vendor APIs (Anthropic, DeepSeek, Google Gemini) behind the
OpenRouter-style slugs the rest of the app uses.

Models stay ``vendor/model`` slugs everywhere (agents, usage records, the
catalog), so Reports keeps pricing them from OpenRouter's public list. When the
agency holds a key for the slug's vendor, the call goes straight to that
vendor's OpenAI-compatible endpoint instead of OpenRouter, and this module
adapts the request on the way out: the slug becomes the vendor's own model id,
and fields only OpenRouter understands are dropped.

Slugs and vendor ids do not always agree (OpenRouter writes
``anthropic/claude-haiku-4.5`` for Anthropic's ``claude-haiku-4-5-20251001``),
so a slug is matched against the model list the vendor serves to the key,
cached per key.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import time
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException

from ..config import get_settings

logger = logging.getLogger(__name__)

ANTHROPIC_VERSION = "2023-06-01"
GEMINI_NATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Vendor (the slug prefix) -> the host its OpenAI-compatible endpoint lives on.
# The registry in providers.py holds the base URLs; this maps a base URL back
# to the vendor whose dialect it speaks.
VENDOR_HOSTS = {
    "anthropic": "api.anthropic.com",
    "deepseek": "api.deepseek.com",
    "google": "generativelanguage.googleapis.com",
}

# How long a vendor's model list is trusted, and how long a failed read is
# remembered before asking again.
MODELS_TTL = 3600.0
MODELS_RETRY = 300.0
_models_cache: dict[tuple[str, str], tuple[float, list[str]]] = {}

# Gemini model families that are not chat models, kept out of the chat offer.
_GEMINI_NON_CHAT = ("embedding", "tts", "image", "live", "audio", "aqa", "veo", "imagen", "robotics", "computer-use", "banana", "transcribe")

_DATE_SUFFIX = re.compile(r"-(\d{8}|\d{2}-\d{2}|\d{4})$")


class ProviderError(HTTPException):
    """The provider answered with an error; ``upstream_status`` is its HTTP status."""

    def __init__(self, upstream_status: int, detail: str):
        super().__init__(status_code=502, detail=detail)
        self.upstream_status = upstream_status


def vendor_for_base_url(base_url: str) -> str | None:
    """The vendor a base URL belongs to, or None for OpenRouter (or anything else)."""
    try:
        host = (urlsplit(base_url).hostname or "").lower()
    except ValueError:
        return None
    for vendor, vendor_host in VENDOR_HOSTS.items():
        if host == vendor_host:
            return vendor
    return None


def slug_vendor(model: str) -> str:
    return model.split("/", 1)[0] if "/" in model else ""


def slug_name(model: str) -> str:
    return model.split("/", 1)[1] if "/" in model else model


def normalize(model_id: str) -> str:
    """A form slugs and vendor ids share: lowercase, no ``models/`` prefix,
    dots as dashes, no release-date suffix."""
    name = model_id.strip().lower()
    if name.startswith("models/"):
        name = name[len("models/"):]
    name = name.replace(".", "-")
    return _DATE_SUFFIX.sub("", name)


def match_model(name: str, natives: list[str]) -> str | None:
    """The vendor id a slug's model part refers to, or None.

    An exact id wins; otherwise ids are compared normalized, and among dated
    releases of the same model the newest one is taken.
    """
    bare = [n[len("models/"):] if n.startswith("models/") else n for n in natives]
    if name in bare:
        return name
    wanted = normalize(name)
    candidates = sorted((n for n in bare if normalize(n) == wanted), reverse=True)
    return candidates[0] if candidates else None


def is_chat_model(vendor: str, native: str) -> bool:
    """Whether a vendor id that OpenRouter does not list is worth offering as
    a chat model. Anthropic and DeepSeek serve only chat models; Gemini's list
    also carries embedding, speech and image models."""
    if vendor != "google":
        return True
    name = native.lower()
    return name.startswith("gemini") and not any(part in name for part in _GEMINI_NON_CHAT)


def is_embedding_model(vendor: str, native: str) -> bool:
    return vendor == "google" and "embedding" in native.lower()


def _models_setting(raw: str) -> list[str]:
    return [model.strip() for model in (raw or "").split(",") if model.strip()]


def fallback_models(base_url: str, model: str) -> list[str]:
    """The chat models to try, in order, when ``model`` fails at a direct
    vendor: the configured fallbacks of that same vendor (same key)."""
    vendor = vendor_for_base_url(base_url)
    if not vendor:
        return []
    return [m for m in _models_setting(get_settings().model_fallbacks) if m != model and slug_vendor(m) == vendor]


def transcription_fallbacks(model: str) -> list[str]:
    """The Gemini transcription models to try after ``model``."""
    return [m for m in _models_setting(get_settings().transcription_models) if m != model and slug_vendor(m) == "google"]


# Upstream answers worth asking another model for: the model was withdrawn,
# the key is rate limited, or the vendor is overloaded or failing.
FALLBACK_STATUSES = frozenset({404, 408, 429, 500, 502, 503, 504})


def should_fall_over(exc: Exception) -> bool:
    if isinstance(exc, ProviderError):
        return exc.upstream_status in FALLBACK_STATUSES
    # Timeouts and dropped connections.
    return isinstance(exc, httpx.TransportError)


def _key_fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


def _models_request(vendor: str, base_url: str, api_key: str) -> tuple[str, dict]:
    url = f"{base_url.rstrip('/')}/models"
    if vendor == "anthropic":
        # The models list is a native endpoint: it authenticates with x-api-key.
        return url, {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}
    return url, {"Authorization": f"Bearer {api_key}"}


async def fetch_vendor_models(vendor: str, base_url: str, api_key: str) -> list[str]:
    """Ask the vendor which models the key can use. Raises on a refused key
    or an unreachable vendor; see ``vendor_models`` for the cached read."""
    url, headers = _models_request(vendor, base_url, api_key)
    params = {"limit": 1000} if vendor == "anthropic" else None
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(url, headers=headers, params=params)
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Could not verify the key: {safe_error(response)}")
    rows = response.json().get("data") or []
    ids = [str(row.get("id") or "") for row in rows if isinstance(row, dict)]
    return [i[len("models/"):] if i.startswith("models/") else i for i in ids if i]


async def vendor_models(vendor: str, base_url: str, api_key: str) -> list[str]:
    """The vendor's model ids for this key, cached; empty when the vendor
    could not be asked (a slug then goes out as written)."""
    cache_key = (vendor, _key_fingerprint(api_key))
    now = time.monotonic()
    cached = _models_cache.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]
    try:
        models = await fetch_vendor_models(vendor, base_url, api_key)
        _models_cache[cache_key] = (now + MODELS_TTL, models)
    except (HTTPException, httpx.HTTPError, ValueError) as exc:
        logger.warning("Could not read the %s model list: %s", vendor, exc)
        models = cached[1] if cached else []
        _models_cache[cache_key] = (now + MODELS_RETRY, models)
    return models


def clear_cache() -> None:
    _models_cache.clear()


async def wire_model(base_url: str, api_key: str, model: str) -> str:
    """The model id to send: the slug itself for OpenRouter, the vendor's own
    id for a direct vendor."""
    vendor = vendor_for_base_url(base_url)
    if not vendor:
        return model
    name = slug_name(model) if slug_vendor(model) == vendor else model
    natives = await vendor_models(vendor, base_url, api_key)
    return match_model(name, natives) or name


async def adapt_chat(base_url: str, api_key: str, payload: dict, sampling: dict) -> tuple[dict, dict]:
    """Fit a chat completion request to the vendor it goes to. A no-op for
    OpenRouter."""
    vendor = vendor_for_base_url(base_url)
    if not vendor:
        return payload, sampling
    payload = {key: value for key, value in payload.items() if key != "usage"}
    payload["model"] = await wire_model(base_url, api_key, payload["model"])
    sampling = dict(sampling)
    effort = get_settings().gemini_reasoning_effort
    if vendor == "google" and effort:
        # Gemini 3 thinks before answering and counts it against max_tokens,
        # which cuts short replies mid-sentence; a minimal effort keeps chat
        # replies whole and quick. In sampling, so a model that refuses it is
        # asked again without it.
        sampling.setdefault("reasoning_effort", effort)
    if vendor == "anthropic":
        # Current Claude models reject any temperature below 1 on this endpoint;
        # leaving it out keeps the model's own default.
        sampling.pop("temperature", None)
        # Claude 5 models think before answering by default, which this
        # endpoint never returns but counts against max_tokens, cutting replies
        # short. In sampling, so a model that refuses the setting is asked
        # again without it.
        thinking = get_settings().anthropic_thinking
        if thinking:
            sampling.setdefault("thinking", {"type": thinking})
        # Strict tool schemas are ignored there anyway.
        if payload.get("tools"):
            payload["tools"] = [_without_strict(tool) for tool in payload["tools"]]
    return payload, sampling


def _without_strict(tool: dict) -> dict:
    function = {key: value for key, value in (tool.get("function") or {}).items() if key != "strict"}
    return {**tool, "function": function}


async def transcribe_with_gemini(api_key: str, model: str, audio: bytes, content_type: str) -> dict:
    """Transcribe audio with Gemini's native API, which takes WhatsApp's ogg
    voice notes as they are (the OpenAI-compatible endpoint has no
    transcription route). A model that fails for a passing reason hands over
    to the next Gemini model in ``TRANSCRIPTION_MODELS``. Returns
    ``{"text", "input_tokens", "output_tokens"}``."""
    candidates = [model, *transcription_fallbacks(model)]
    for index, candidate in enumerate(candidates):
        last = index == len(candidates) - 1
        try:
            return await _gemini_transcription(api_key, candidate, audio, content_type, 120 if last else get_settings().model_attempt_timeout)
        except (ProviderError, httpx.TransportError) as exc:
            if last or not should_fall_over(exc):
                if isinstance(exc, httpx.TransportError):
                    raise HTTPException(status_code=502, detail="Could not reach the transcription provider.") from exc
                raise
            logger.warning("Transcription with %s failed (%s); falling over to %s", candidate, getattr(exc, "detail", type(exc).__name__), candidates[index + 1])
    raise AssertionError("unreachable")


async def _gemini_transcription(api_key: str, model: str, audio: bytes, content_type: str, timeout: float) -> dict:
    natives = await vendor_models("google", f"{GEMINI_NATIVE_BASE}/openai", api_key)
    name = slug_name(model) if slug_vendor(model) == "google" else model
    native = match_model(name, natives) or name
    mime = (content_type or "audio/ogg").split(";")[0].strip() or "audio/ogg"
    body = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": "Transcribe this audio verbatim, in the language it is spoken. Reply with the transcript only."},
                {"inline_data": {"mime_type": mime, "data": base64.b64encode(audio).decode()}},
            ],
        }],
    }
    url = f"{GEMINI_NATIVE_BASE}/models/{native}:generateContent"
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, headers={"x-goog-api-key": api_key}, json=body)
    if response.status_code >= 400:
        raise ProviderError(response.status_code, f"Transcription failed: {safe_error(response)}")
    try:
        data = response.json()
        parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    except (ValueError, AttributeError) as exc:
        raise HTTPException(status_code=502, detail="Invalid transcription response.") from exc
    usage = data.get("usageMetadata") or {}
    return {
        "text": "".join(_part_text(part) for part in parts if isinstance(part, dict)).strip(),
        "input_tokens": int(usage.get("promptTokenCount") or 0),
        "output_tokens": int(usage.get("candidatesTokenCount") or 0),
    }


def _part_text(part: dict) -> str:
    """A part's text: plain text from a chat model, ``audioTranscription``
    from Gemini's dedicated transcription models."""
    transcription = part.get("audioTranscription")
    if isinstance(transcription, dict):
        return str(transcription.get("text") or "")
    return str(part.get("text") or "")


async def test_key(vendor: str, base_url: str, api_key: str) -> dict:
    """Verify a vendor key by listing the models it can use."""
    try:
        models = await fetch_vendor_models(vendor, base_url, api_key)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Could not connect to the provider") from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Invalid response from the provider") from exc
    _models_cache[(vendor, _key_fingerprint(api_key))] = (time.monotonic() + MODELS_TTL, models)
    return {"ok": True, "message": f"Key verified. {len(models)} models available.", "models": []}


def safe_error(response: httpx.Response) -> str:
    try:
        data = response.json()
        # Gemini wraps its error in a one-item list.
        if isinstance(data, list) and data:
            data = data[0]
        error = data.get("error") if isinstance(data, dict) else None
        message = (error.get("message") if isinstance(error, dict) else None) or (data.get("message") if isinstance(data, dict) else None)
        if isinstance(message, str):
            return message[:500]
    except ValueError:
        pass
    return f"HTTP {response.status_code}"
