"""Where a system prompt stops being the same from request to request.

``CACHE_BOUNDARY`` splits a system prompt in two: before it, what stays the
same from turn to turn and across an agent's conversations; after it, what
changes every turn (the time, what was retrieved or captured for this
conversation, and whatever a caller appends: the contact, their bookings). A
provider that caches prompts caches the first part (see ``anthropic_native``);
every other request has the marker replaced by a paragraph break.
"""

CACHE_BOUNDARY = "\n\n<!-- cache boundary -->\n\n"


def without_boundary(text):
    """``text`` with the marker turned back into a paragraph break."""
    return text.replace(CACHE_BOUNDARY, "\n\n") if isinstance(text, str) else text


def insert_stable(system: str, block: str) -> str:
    """``system`` with ``block`` added to its fixed part, before the boundary."""
    if not block:
        return system
    if CACHE_BOUNDARY not in system:
        return f"{system}\n\n{block}"
    fixed, changing = system.split(CACHE_BOUNDARY, 1)
    return f"{fixed}\n\n{block}{CACHE_BOUNDARY}{changing}"
