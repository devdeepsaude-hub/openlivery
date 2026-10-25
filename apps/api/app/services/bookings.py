"""An outside booking service, shown beside a conversation in the portal.

Optional: with ``BOOKINGS_URL`` set, the portal lists the contact's bookings
next to the thread, with Cancel and Mark as paid. OpenLivery keeps no booking
of its own: the service owns the bookings and their wording, and answers

    GET  {url}/bookings?phone=<digits>        -> {"bookings": [booking, ...]}
    POST {url}/bookings/{id}/cancel  {"phone": ..., "by": ...}  -> same list, refreshed
    POST {url}/bookings/{id}/paid    {"phone": ..., "by": ...}  -> same list, refreshed

each with ``BOOKINGS_TOKEN`` as a Bearer token. The contact's phone travels
with every action, so the service can refuse a booking that is not theirs. A
booking is ``{"id", "title", "when", "status", "detail", "can_cancel",
"can_mark_paid"}``, its text already in the business's language.
"""

from urllib.parse import quote

import httpx
from fastapi import HTTPException

from ..config import get_settings
from .contacts import phone_from_chat_id

TIMEOUT_SECONDS = 15.0
ACTIONS = ("cancel", "paid")
# The agent's prompt carries the contact's bookings as they are now, so it never
# confirms or reminds of one the team cancelled since the conversation said so.
# LLM-facing, in the language of the agent's prompt like the rest of it.
_CONTEXT = {
    "es": ("RESERVAS DE ESTA PERSONA AHORA (consultadas en el sistema de reservas en este momento; valen más que la conversación):",
           "Ninguna reserva activa.",
           "Si la conversación habla de una reserva que no está en esta lista, fue cancelada o expiró: no la confirmes ni la recuerdes como agendada."),
    "en": ("THIS PERSON'S BOOKINGS NOW (looked up in the booking system just now; they outweigh the conversation):",
           "No active booking.",
           "If the conversation mentions a booking that is not on this list, it was cancelled or expired: do not confirm it or remind them of it as booked."),
    "pt": ("RESERVAS DESTA PESSOA AGORA (consultadas no sistema de reservas neste momento; valem mais do que a conversa):",
           "Nenhuma reserva ativa.",
           "Se a conversa fala de uma reserva que não está nesta lista, ela foi cancelada ou expirou: não a confirme nem a lembre como marcada."),
}


def enabled() -> bool:
    return bool(get_settings().bookings_url.strip())


def contact_phone(conversation) -> str:
    """The phone bookings are looked up by: the contact's, else the chat's own."""
    if conversation.contact and conversation.contact.phone:
        return conversation.contact.phone
    return phone_from_chat_id(conversation.external_chat_id) or ""


async def _request(method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> httpx.Response:
    settings = get_settings()
    token = settings.bookings_token.strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
        return await http.request(method, settings.bookings_url.strip().rstrip("/") + path, params=params, json=body, headers=headers)


def _message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    if isinstance(payload, dict):
        for key in ("detail", "error", "erro", "message"):
            if isinstance(payload.get(key), str):
                return payload[key]
    return ""


def _booking(item: dict) -> dict:
    return {
        "id": str(item["id"]),
        "title": str(item.get("title") or ""),
        "when": str(item.get("when") or ""),
        "status": str(item.get("status") or ""),
        "detail": str(item["detail"]) if item.get("detail") else None,
        "can_cancel": bool(item.get("can_cancel")),
        "can_mark_paid": bool(item.get("can_mark_paid")),
    }


def _bookings(response: httpx.Response) -> list[dict]:
    if response.status_code >= 400:
        # The service's own words reach the person (it refused, and says why);
        # its failures read as unavailability.
        if response.status_code >= 500:
            raise HTTPException(status_code=502, detail="The booking service failed. Try again in a moment.")
        raise HTTPException(status_code=409, detail=_message(response) or "The booking service refused this.")
    try:
        items = response.json().get("bookings")
    except (ValueError, AttributeError):
        items = None
    if not isinstance(items, list):
        raise HTTPException(status_code=502, detail="The booking service answered something unexpected.")
    return [_booking(item) for item in items if isinstance(item, dict) and item.get("id")]


async def list_for(phone: str) -> list[dict]:
    """The bookings the service holds for this phone."""
    try:
        response = await _request("GET", "/bookings", params={"phone": phone})
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="The booking service did not answer. Try again in a moment.") from exc
    return _bookings(response)


async def on_file(conversation) -> bool:
    """Whether the service holds any booking for the conversation's contact.
    True when it cannot tell (no service, no phone, or no answer), so a check
    that relies on it never acts on a guess."""
    phone = contact_phone(conversation)
    if not enabled() or not phone:
        return True
    try:
        return bool(await list_for(phone))
    except HTTPException:
        return True


async def act(booking_id: str, action: str, phone: str, by: str) -> list[dict]:
    """Cancel a booking or mark it paid on behalf of ``by``; returns the refreshed list."""
    if action not in ACTIONS:
        raise HTTPException(status_code=404, detail="Unknown booking action")
    try:
        response = await _request(
            "POST", f"/bookings/{quote(booking_id, safe='')}/{action}", body={"phone": phone, "by": by}
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="The booking service did not answer. Try again in a moment.") from exc
    return _bookings(response)


async def prompt_context(conversation, lang: str) -> str:
    """The contact's bookings right now, worded for the agent's prompt. Empty
    with no booking service, no phone, or when the service fails: the reply
    goes out without it rather than not at all."""
    phone = contact_phone(conversation)
    if not enabled() or not phone:
        return ""
    try:
        items = await list_for(phone)
    except HTTPException:
        return ""
    title, empty, rule = _CONTEXT[lang if lang in _CONTEXT else "es"]
    lines = [f"- {item['title']}, {item['when']}: {item['status']}" for item in items] or [empty]
    return "\n".join([title, *lines, rule])
