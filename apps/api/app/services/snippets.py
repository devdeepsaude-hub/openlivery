"""Fixed messages an agent sends word for word.

An operator writes a block in any of the agent's texts:

    [[prices]]
    ...the exact message, with its emojis and line breaks...
    [[/prices]]

The system prompt tells the model to answer with the marker ``[[prices]]``
alone on a line when that message is due, and the reply goes out with the
marker replaced by the block. Asked to copy a long message, models drop a line,
add one or reword it; this way the wording is always the operator's. A marker
naming no block, and any stray closing marker, are dropped.
"""

import re

_NAME = r"[a-z0-9_]{1,40}"
_BLOCK = re.compile(rf"^[ \t]*\[\[({_NAME})\]\][ \t]*\n(.*?)\n[ \t]*\[\[/\1\]\][ \t]*$", re.MULTILINE | re.DOTALL | re.IGNORECASE)
_MARKER = re.compile(rf"\[\[(/?)({_NAME})\]\]", re.IGNORECASE)

# The agent texts a block may live in, as written in the panel.
FIELDS = (
    "instructions", "personality", "brief_summary", "brief_products",
    "brief_audience", "brief_policies", "brief_dos", "brief_donts",
)


def blocks(agent) -> dict[str, str]:
    """Every block the agent's texts define, by lowercase name; the first one
    wins when two share a name."""
    found: dict[str, str] = {}
    for field in FIELDS:
        for match in _BLOCK.finditer(getattr(agent, field, "") or ""):
            found.setdefault(match.group(1).lower(), match.group(2).strip("\n"))
    return found


def expand(text: str, found: dict[str, str]) -> str:
    """``text`` with each marker replaced by its block, set apart as its own
    paragraphs."""
    if not text or "[[" not in text:
        return text

    def known(match: re.Match) -> bool:
        return not match.group(1) and match.group(2).lower() in found

    # First the model's own text: markers that name nothing go, with the gap they leave.
    result = _MARKER.sub(lambda match: match.group(0) if known(match) else "", text)
    result = re.sub(r"[ \t]{2,}", " ", result)
    result = _MARKER.sub(lambda match: f"\n\n{found[match.group(2).lower()]}\n\n", result)
    result = re.sub(r"[ \t]+\n", "\n", result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()
