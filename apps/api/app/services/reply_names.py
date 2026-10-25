"""Keep the contact's name to the greeting.

Models tend to address the contact by name in message after message ("Got it,
Ana."), which reads like a script, and prompts alone do not stop every model
from doing it. With ``REPLY_NAME_ONLY_IN_GREETING`` on, once the conversation
has been answered the contact's first name is taken out of the spots where it
only addresses them: right after a comma and before punctuation ("Got it,
Ana." becomes "Got it."), or opening a sentence before a comma ("Ana, tell
me..." becomes "Tell me..."). Anywhere else, such as a team member who shares
the name, it stays.
"""

import re

from ..config import get_settings
from ..models import Conversation

# Words a display name may start with that are not the person's name.
_TITLES = {"dr", "dra", "sr", "sra", "srta", "prof", "profa", "mr", "mrs", "ms"}
_WORD = re.compile(r"[^\W\d_]{2,}")


def first_name(display_name: str | None) -> str:
    """The first name in a contact's display name ("🌻 Carla Souza" -> "Carla"),
    or "" when there is none worth matching."""
    for word in _WORD.findall(display_name or ""):
        if word.lower() not in _TITLES:
            return word
    return ""


def drop_name(text: str, name: str) -> str:
    """``text`` without ``name`` where it only addresses the reader."""
    if not text or not name:
        return text
    escaped = re.escape(name)
    # ", Ana." / ", Ana," / ", Ana!" / ", Ana" at the end: drop ", Ana", keep the punctuation.
    text = re.sub(rf",\s*{escaped}\b(?=\s*[.!?,…:;)]|\s*$)", "", text, flags=re.IGNORECASE | re.MULTILINE)
    # "Ana, tell me..." opening the text, a line or a sentence: drop it and
    # capitalize what now opens the sentence.
    text = re.sub(
        rf"(^|(?<=[.!?…]\s))[ \t]*{escaped},\s*(\w)",
        lambda match: match.group(1) + match.group(2).upper(),
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    return text


def keep_name_to_greeting(text: str, conversation: Conversation) -> str:
    """The reply as it should go out: unchanged for the first answer of a
    conversation (the greeting may use the name), without the vocative name
    after that, when the setting is on."""
    if not text or not get_settings().reply_name_only_in_greeting or conversation.first_reply_at is None:
        return text
    return drop_name(text, first_name(conversation.contact_name))
