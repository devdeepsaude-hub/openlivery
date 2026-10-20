"""Vendor keys used directly (Anthropic, DeepSeek, Google Gemini).

Agents keep OpenRouter slugs; a slug whose vendor has a key in use is served
by that vendor, with the request fitted to its dialect on the way out.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from conftest import TestingSession

from app.models import Agent
from app.services import media, providers, vendors


@pytest.fixture(autouse=True)
def vendor_lists(monkeypatch):
    """What each vendor serves to any key, so no test reaches a vendor."""
    served = {
        "anthropic": ["claude-sonnet-5-20260901", "claude-haiku-4-5-20251001"],
        "deepseek": ["deepseek-flash", "deepseek-v4-pro"],
        "google": ["gemini-3.8-flash", "gemini-embedding-001", "gemini-2.5-flash-preview-tts"],
    }

    async def _models(vendor, base_url, api_key):
        return served[vendor]

    monkeypatch.setattr(vendors, "vendor_models", _models)
    vendors.clear_cache()
    return served


def _agent(client: TestClient, model: str) -> str:
    customer = client.post("/api/clients", json={"name": "Clinica", "is_active": True}).json()
    agent = client.post(
        "/api/agents",
        json={"client_id": customer["id"], "provider": "openrouter", "model": model, "name": "Recepcao", "instructions": "", "personality": ""},
    ).json()
    return agent["id"]


def test_slugs_match_the_vendor_ids_they_name():
    assert vendors.match_model("claude-haiku-4.5", ["claude-haiku-4-5-20251001", "claude-sonnet-5"]) == "claude-haiku-4-5-20251001"
    assert vendors.match_model("gemini-3.8-flash", ["models/gemini-3.8-flash"]) == "gemini-3.8-flash"
    assert vendors.match_model("claude-sonnet-4", ["claude-sonnet-4-5-20250929"]) is None
    # Among dated releases of one model, the newest wins.
    assert vendors.match_model("claude-sonnet-5", ["claude-sonnet-5-20260101", "claude-sonnet-5-20260901"]) == "claude-sonnet-5-20260901"


def test_base_urls_name_their_vendor():
    assert vendors.vendor_for_base_url("https://api.anthropic.com/v1") == "anthropic"
    assert vendors.vendor_for_base_url("https://generativelanguage.googleapis.com/v1beta/openai") == "google"
    assert vendors.vendor_for_base_url("https://openrouter.ai/api/v1") is None


def test_openrouter_requests_go_out_unchanged():
    payload = {"model": "anthropic/claude-sonnet-5", "messages": [], "usage": {"include": True}}
    sent, sampling = asyncio.run(vendors.adapt_chat("https://openrouter.ai/api/v1", "key", payload, {"temperature": 0.7}))
    assert sent == payload and sampling == {"temperature": 0.7}


def test_anthropic_requests_are_fitted_to_its_endpoint():
    payload = {
        "model": "anthropic/claude-sonnet-5",
        "messages": [{"role": "user", "content": "oi"}],
        "usage": {"include": True},
        "tools": [{"type": "function", "function": {"name": "agendar", "parameters": {}, "strict": False}}],
    }
    sent, sampling = asyncio.run(vendors.adapt_chat("https://api.anthropic.com/v1", "key", payload, {"temperature": 0.7, "max_tokens": 500}))
    assert sent["model"] == "claude-sonnet-5-20260901"
    assert "usage" not in sent and "strict" not in sent["tools"][0]["function"]
    # Current Claude models refuse a temperature below 1 there; the cap stays.
    assert sampling == {"max_tokens": 500}
    # The caller's payload is left as it was.
    assert payload["model"] == "anthropic/claude-sonnet-5" and "usage" in payload


def test_an_agent_is_served_by_its_vendor_once_the_key_is_set(authenticated_client: TestClient):
    client = authenticated_client
    claude_id = _agent(client, "anthropic/claude-sonnet-5")
    gpt_id = _agent(client, "openai/gpt-4.1")
    with TestingSession() as db:
        assert providers.resolve_agent_credentials(db, db.get(Agent, claude_id)) is None

    assert client.put("/api/providers/anthropic", json={"api_key": "sk-ant-test"}).status_code == 200
    with TestingSession() as db:
        claude, gpt = db.get(Agent, claude_id), db.get(Agent, gpt_id)
        assert providers.resolve_agent_credentials(db, claude) == ("https://api.anthropic.com/v1", "sk-ant-test")
        # No key for that vendor and none for OpenRouter: nothing serves it.
        assert providers.resolve_agent_credentials(db, gpt) is None

    client.put("/api/providers/openrouter", json={"api_key": "sk-or-test"})
    with TestingSession() as db:
        claude, gpt = db.get(Agent, claude_id), db.get(Agent, gpt_id)
        assert providers.resolve_agent_credentials(db, claude)[0] == "https://api.anthropic.com/v1"
        assert providers.resolve_agent_credentials(db, gpt) == ("https://openrouter.ai/api/v1", "sk-or-test")

    # A paused vendor key hands the model back to OpenRouter.
    client.patch("/api/providers/anthropic", json={"enabled": False})
    with TestingSession() as db:
        assert providers.resolve_agent_credentials(db, db.get(Agent, claude_id))[0] == "https://openrouter.ai/api/v1"


def test_the_offer_is_what_the_vendor_keys_serve(authenticated_client: TestClient):
    client = authenticated_client
    client.put("/api/providers/deepseek", json={"api_key": "sk-ds"})
    client.put("/api/providers/google", json={"api_key": "AIza-test"})
    offer = client.get("/api/catalog/available").json()
    chat = offer["chat"]["openrouter"]
    assert "deepseek/deepseek-flash" in chat and "deepseek/deepseek-v4-pro" in chat
    assert "google/gemini-3.8-flash" in chat
    assert not any(model.startswith(("openai/", "anthropic/")) for model in chat)
    assert "google/gemini-2.5-flash-preview-tts" not in chat
    assert offer["embedding"] == ["google/gemini-embedding-001"]

    # With an OpenRouter key as well, everything is on offer again.
    client.put("/api/providers/openrouter", json={"api_key": "sk-or"})
    assert "openai/gpt-4.1" in client.get("/api/catalog/available").json()["chat"]["openrouter"]


def test_a_vendor_key_is_verified_against_its_model_list(authenticated_client: TestClient, monkeypatch):
    async def _fetch(vendor, base_url, api_key):
        assert (vendor, api_key) == ("anthropic", "sk-ant-test")
        return ["claude-sonnet-5-20260901"]

    monkeypatch.setattr(vendors, "fetch_vendor_models", _fetch)
    response = authenticated_client.post("/api/providers/anthropic/validate", json={"api_key": "sk-ant-test"})
    assert response.status_code == 200 and response.json()["ok"] is True


class _GeminiClient:
    seen: dict = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, headers, json):
        _GeminiClient.seen = {"url": url, "headers": headers, "json": json}

        class _Response:
            status_code = 200

            def json(self):
                return {
                    "candidates": [{"content": {"parts": [{"text": " quero marcar consulta "}]}}],
                    "usageMetadata": {"promptTokenCount": 40, "candidatesTokenCount": 6},
                }

        return _Response()


def test_gemini_transcribes_whatsapp_voice_notes_natively(monkeypatch):
    monkeypatch.setattr(vendors.httpx, "AsyncClient", lambda timeout: _GeminiClient())
    result = asyncio.run(media.transcribe_audio(
        "https://generativelanguage.googleapis.com/v1beta/openai", "AIza-test", "google/gemini-3.8-flash",
        b"ogg-bytes", "audio.ogg", "audio/ogg; codecs=opus",
    ))
    assert result.text == "quero marcar consulta" and (result.input_tokens, result.output_tokens) == (40, 6)
    seen = _GeminiClient.seen
    assert seen["url"].endswith("/models/gemini-3.8-flash:generateContent")
    assert seen["headers"] == {"x-goog-api-key": "AIza-test"}
    assert seen["json"]["contents"][0]["parts"][1]["inline_data"]["mime_type"] == "audio/ogg"


def test_vendors_without_audio_refuse_to_transcribe():
    with pytest.raises(Exception) as excinfo:
        asyncio.run(media.transcribe_audio("https://api.deepseek.com", "sk-ds", "deepseek/deepseek-flash", b"ogg"))
    assert "does not transcribe" in str(getattr(excinfo.value, "detail", excinfo.value))
