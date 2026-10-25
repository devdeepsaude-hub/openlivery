"""A reply must not tell the contact something is booked when it is not.

Models sometimes confirm a slot ("it's booked for Wednesday at 10") although
no booking tool ran. With ``BOOKING_EVIDENCE_TOOLS`` set to the tools whose
success backs such a claim (the one that books, the one that looks bookings
up), a reply that claims a booking while none of them succeeded in the same
turn, for a contact with no booking on file, is written again once, with a
note in the system prompt saying no booking exists. If the second answer still claims one, the
claiming sentences are taken out, and if nothing is left a neutral line asks
the contact to confirm the slot.

The claim and hedge patterns are per prompt language: a sentence counts as a
claim when it states the booking as done, not when it explains what will
happen ("it is confirmed once paid") or asks ("shall I book it?").
"""

from collections.abc import Awaitable, Callable
from dataclasses import replace
import re

from ..config import get_settings
from .ai import Completion

_FLAGS = re.IGNORECASE

_CLAIM = {
    "pt": re.compile(
        r"\b(?:ficou|fica|est[aá]|t[aá]|foi|j[aá] est[aá]|j[aá] ficou)\s+(?:tudo\s+)?"
        r"(?:reservad|agendad|marcad|confirmad|guardad|garantid)[oa]s?\b"
        r"|\b(?:reservei|agendei|marquei|guardei)\b",
        _FLAGS,
    ),
    "es": re.compile(
        r"\b(?:qued[oó]|queda|est[aá]|ya est[aá]|fue)\s+(?:reservad|agendad|confirmad|apartad|guardad)[oa]s?\b"
        r"|\b(?:reserv[eé]|agend[eé]|apart[eé]|guard[eé])\b",
        _FLAGS,
    ),
    "en": re.compile(
        r"\b(?:is|was|are|were|has been|have been|it's|you're|you are)\s+(?:all\s+)?(?:booked|reserved|scheduled|confirmed|held)\b"
        r"|\bI(?:'ve| have)?\s+(?:booked|reserved|scheduled|held)\b",
        _FLAGS,
    ),
}
_HEDGE = {
    "pt": re.compile(r"\?|\b(?:quando|assim que|depois que|ap[oó]s|se|caso|posso|podemos|quer que|antes de)\b", _FLAGS),
    "es": re.compile(r"\?|\b(?:cuando|en cuanto|despu[eé]s de|si|puedo|podemos|quieres que|antes de)\b", _FLAGS),
    "en": re.compile(r"\?|\b(?:when|once|after|if|can|could|shall|would you|I'll|I will|before|to book)\b", _FLAGS),
}
# LLM-facing, in the prompt's language like the rest of the prompt.
_NOTE = {
    "pt": ("AVISO DO SISTEMA: a resposta que você ia mandar diz que um horário foi reservado, marcado ou confirmado, "
           "mas nenhuma reserva foi feita. Escreva a resposta de novo sem afirmar reserva. Se a pessoa escolheu um "
           "horário, siga o próximo passo do atendimento ou use a ferramenta de reserva antes de dizer que está reservado."),
    "es": ("AVISO DEL SISTEMA: la respuesta que ibas a enviar dice que un horario quedó reservado o confirmado, pero no "
           "se hizo ninguna reserva. Escribe la respuesta de nuevo sin afirmar una reserva. Si la persona eligió un "
           "horario, sigue el próximo paso de la atención o usa la herramienta de reserva antes de decir que está reservado."),
    "en": ("SYSTEM NOTICE: the reply you were about to send says a slot was booked or confirmed, but no booking was made. "
           "Write the reply again without claiming a booking. If the person chose a slot, take the next step of the "
           "conversation or use the booking tool before saying it is booked."),
}
# Contact-facing, only when nothing is left of a reply once its claims are out.
_FALLBACK = {
    "pt": "Me confirma, por favor, o dia e o horário que você prefere, para eu seguir com a reserva?",
    "es": "¿Me confirmas, por favor, el día y el horario que prefieres, para seguir con la reserva?",
    "en": "Could you confirm the day and time you prefer, so I can go ahead with the booking?",
}


def evidence_tools() -> set[str]:
    return {name.strip() for name in get_settings().booking_evidence_tools.split(",") if name.strip()}


def _lang(lang: str) -> str:
    return lang if lang in _CLAIM else "en"


def _sentences(text: str) -> list[str]:
    return [part for part in re.split(r"(?<=[.!?…])[ \t]+|\n+", text) if part.strip()]


def claims_booking(text: str, lang: str) -> bool:
    """Whether some sentence of ``text`` states a booking as done."""
    lang = _lang(lang)
    return any(_CLAIM[lang].search(s) and not _HEDGE[lang].search(s) for s in _sentences(text or ""))


def backed(completion: Completion, tools: set[str]) -> bool:
    """Whether a booking tool succeeded while this reply was being written."""
    return any(call.get("name") in tools and not call.get("is_error") for call in completion.tool_calls or [])


def drop_claims(text: str, lang: str) -> str:
    """``text`` without the sentences that state a booking as done; paragraphs
    left empty go too."""
    lang = _lang(lang)
    paragraphs = []
    for paragraph in re.split(r"\n\s*\n", text or ""):
        lines = []
        for line in paragraph.split("\n"):
            kept = [s for s in re.split(r"(?<=[.!?…])[ \t]+", line)
                    if not (_CLAIM[lang].search(s) and not _HEDGE[lang].search(s))]
            if " ".join(kept).strip():
                lines.append(" ".join(kept).strip())
        if lines:
            paragraphs.append("\n".join(lines))
    return "\n\n".join(paragraphs)


def _with_note(messages: list[dict], note: str) -> list[dict]:
    """``messages`` with ``note`` added to the system prompt: a system message in
    the middle of a conversation is not accepted by every provider."""
    amended = list(messages)
    for index, message in enumerate(amended):
        if message["role"] == "system":
            amended[index] = {**message, "content": f"{message['content']}\n\n{note}"}
            return amended
    return [{"role": "system", "content": note}, *amended]


def _merged(first: Completion, second: Completion) -> Completion:
    """The second answer, carrying the cost and the tool calls of both."""
    costs = [c for c in (first.cost_usd, second.cost_usd) if c is not None]
    return replace(
        second,
        input_tokens=first.input_tokens + second.input_tokens,
        output_tokens=first.output_tokens + second.output_tokens,
        cached_tokens=first.cached_tokens + second.cached_tokens,
        reasoning_tokens=first.reasoning_tokens + second.reasoning_tokens,
        cost_usd=sum(costs) if costs else None,
        tool_calls=[*(first.tool_calls or []), *(second.tool_calls or [])] or None,
    )


async def guard_booking_claims(
    complete: Callable[[list[dict]], Awaitable[Completion]],
    messages: list[dict],
    completion: Completion,
    *,
    lang: str,
    has_booking: Callable[[], Awaitable[bool]] | None = None,
) -> Completion:
    """``completion`` as it may go out. ``complete`` writes a reply for a list of
    messages; ``has_booking`` says whether the contact has a booking on file
    (left out where there is no contact to look up, as in the playground)."""
    tools = evidence_tools()
    if not tools or backed(completion, tools) or not claims_booking(completion.text, lang):
        return completion
    if has_booking is not None and await has_booking():
        return completion
    lang = _lang(lang)
    retry = await complete(_with_note(messages, _NOTE[lang]))
    result = _merged(completion, retry)
    if not backed(result, tools) and claims_booking(result.text, lang):
        result.text = drop_claims(result.text, lang) or _FALLBACK[lang]
    return result
