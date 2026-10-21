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

TIMEOUT_SECONDS = 15.0
ACTIONS = ("cancel", "paid")


def enabled() -> bool:
    return bool(get_settings().bookings_url.strip())


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
