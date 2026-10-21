"""The outside booking service shown beside a conversation in the portal."""

from unittest.mock import AsyncMock

import httpx
from fastapi.testclient import TestClient

from app.config import get_settings
from app.services import ai as ai_service
from app.services import bookings as bookings_service
from app.services import whatsapp as whatsapp_service
from app.services import whatsapp_inbound as whatsapp_inbound_service

BOOKING = {
    "id": "R1", "title": "Pré-sessão com Erika", "when": "quinta 22/10 às 16h30", "status": "Confirmada",
    "can_cancel": True, "can_mark_paid": False, "something_else": "ignored",
}


def _conversation(client: TestClient, monkeypatch, company: str = "Agenda Co"):
    customer = client.post("/api/clients", json={"name": company, "is_active": True}).json()
    slug = customer["portal_slug"]
    client.post(f"/api/clients/{customer['id']}/portal-users",
                json={"name": "Ana", "email": f"ana@{slug}.com", "password": "secure-portal"})
    client.patch(f"/api/clients/{customer['id']}/portal", json={"portal_enabled": True})
    client.put("/api/providers/openrouter", json={"api_key": "secret"})
    agent = client.post("/api/agents", json={
        "client_id": customer["id"], "provider": "openrouter", "model": "gpt-4.1-mini", "name": "Mari",
        "instructions": "", "personality": "", "is_active": True,
    }).json()
    channel = client.put(f"/api/whatsapp/channels/{customer['id']}", json={"agent_id": agent["id"]}).json()
    monkeypatch.setattr(whatsapp_service, "bridge_command", AsyncMock(return_value={}))
    monkeypatch.setattr(whatsapp_inbound_service, "run_completion", AsyncMock(return_value=ai_service.Completion(text="Oi!")))
    body = client.post(
        f"/api/internal/whatsapp/channels/{channel['id']}/inbound",
        headers={"X-Bridge-Token": get_settings().whatsapp_bridge_token},
        json={"external_message_id": "wa-1", "remote_jid": "573001112233@s.whatsapp.net", "sender_name": "Sam", "text": "oi"},
    ).json()
    assert client.post(f"/api/portal/{slug}/login", json={"email": f"ana@{slug}.com", "password": "secure-portal"}).status_code == 200
    return slug, body["conversation_id"]


def _service(monkeypatch, *responses):
    """Point BOOKINGS_URL at a fake service answering ``responses`` in turn."""
    settings = get_settings()
    monkeypatch.setattr(settings, "bookings_url", "http://agenda.example")
    monkeypatch.setattr(settings, "bookings_token", "tok")
    calls, queue = [], list(responses)

    async def fake(method, path, *, params=None, body=None):
        calls.append((method, path, params, body))
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(bookings_service, "_request", fake)
    return calls


def test_without_a_booking_service_the_panel_stays_hidden(authenticated_client: TestClient, monkeypatch):
    slug, conversation_id = _conversation(authenticated_client, monkeypatch)
    monkeypatch.setattr(get_settings(), "bookings_url", "")
    assert authenticated_client.get(f"/api/portal/{slug}/conversations/{conversation_id}/bookings").json() == {
        "enabled": False, "bookings": [],
    }
    assert authenticated_client.post(f"/api/portal/{slug}/conversations/{conversation_id}/bookings/R1/cancel").status_code == 404


def test_the_contacts_bookings_and_actions_go_to_the_service(authenticated_client: TestClient, monkeypatch):
    client = authenticated_client
    slug, conversation_id = _conversation(client, monkeypatch, "Lista Co")
    calls = _service(monkeypatch, httpx.Response(200, json={"bookings": [BOOKING, {"title": "without an id"}]}))
    base = f"/api/portal/{slug}/conversations/{conversation_id}/bookings"

    listed = client.get(base).json()
    assert listed == {"enabled": True, "bookings": [{
        "id": "R1", "title": "Pré-sessão com Erika", "when": "quinta 22/10 às 16h30", "status": "Confirmada",
        "detail": None, "can_cancel": True, "can_mark_paid": False,
    }]}
    method, path, params, body = calls[0]
    assert (method, path, body) == ("GET", "/bookings", None) and "3001112233" in params["phone"]

    # Each action names the contact's phone and the person who acted.
    assert client.post(f"{base}/R1/cancel").status_code == 200
    assert calls[-1][:2] == ("POST", "/bookings/R1/cancel") and calls[-1][3]["by"] == "Ana"
    assert calls[-1][3]["phone"] == params["phone"]
    assert client.post(f"{base}/R1/paid").status_code == 200 and calls[-1][1] == "/bookings/R1/paid"
    assert client.post(f"{base}/R1/delete").status_code == 404
    assert len(calls) == 3


def test_the_service_refusal_reaches_the_person(authenticated_client: TestClient, monkeypatch):
    client = authenticated_client
    slug, conversation_id = _conversation(client, monkeypatch, "Recusa Co")
    base = f"/api/portal/{slug}/conversations/{conversation_id}/bookings"
    _service(monkeypatch, httpx.Response(409, json={"erro": "essa reserva não é deste telefone"}))
    refused = client.post(f"{base}/R9/cancel")
    assert refused.status_code == 409 and refused.json()["detail"] == "essa reserva não é deste telefone"
    _service(monkeypatch, httpx.ConnectError("down"))
    assert client.get(base).status_code == 502
    _service(monkeypatch, httpx.Response(500, text="boom"))
    assert client.post(f"{base}/R1/paid").status_code == 502


def test_another_clients_conversation_is_not_found(authenticated_client: TestClient, monkeypatch):
    client = authenticated_client
    _, conversation_id = _conversation(client, monkeypatch, "Uma Co")
    other_slug, _ = _conversation(client, monkeypatch, "Outra Co")
    _service(monkeypatch, httpx.Response(200, json={"bookings": [BOOKING]}))
    assert client.get(f"/api/portal/{other_slug}/conversations/{conversation_id}/bookings").status_code == 404
