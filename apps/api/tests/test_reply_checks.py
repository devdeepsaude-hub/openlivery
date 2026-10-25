"""A reply must not tell the contact something is booked when it is not."""

import asyncio

from app.config import get_settings
from app.services.ai import Completion
from app.services.reply_checks import claims_booking, drop_claims, guard_booking_claims

BOOKING_TOOLS = "reservar_horario,minhas_reservas"


def test_claims_are_told_apart_from_offers_and_explanations():
    for claim in (
        "Perfeito, ficou reservado o horário de quarta, 28/10, às 10h com a Karine 😊",
        "Pronto, reservei quarta às 10h.",
        "Sua pré-sessão está marcada para segunda, às 17h.",
        "Combinado! Está tudo agendado pra quarta.",
    ):
        assert claims_booking(claim, "pt"), claim
    for not_a_claim in (
        "Antes de reservar, deixa eu te passar os valores.",
        "O horário fica confirmado quando o pagamento for feito.",
        "Quer que eu deixe reservado por 24 horas?",
        "Se preferir, o horário fica guardado por 24 horas.",
        "Confirmei aqui que a Erika tem horário à tarde.",
        "Tenho quarta às 10h. Qual fica melhor?",
    ):
        assert not claims_booking(not_a_claim, "pt"), not_a_claim
    assert claims_booking("You're all booked for Wednesday.", "en")
    assert not claims_booking("It is confirmed once you pay.", "en")
    assert claims_booking("Ya quedó reservado el miércoles.", "es")


def test_dropping_claims_keeps_the_rest_of_the_reply():
    text = ("Perfeito, ficou reservado o horário de quarta às 10h 😊\n\n"
            "Os valores são os seguintes:\n\n✨ Pagamento semanal: R$ 150\n\nQual opção faz mais sentido?")
    assert drop_claims(text, "pt") == "Os valores são os seguintes:\n\n✨ Pagamento semanal: R$ 150\n\nQual opção faz mais sentido?"


def _scripted(*replies):
    calls = []

    async def complete(messages):
        calls.append(messages)
        return replies[len(calls) - 1]

    return complete, calls


def _guard(completion, complete, *, has_booking=None):
    messages = [{"role": "system", "content": "Prompt."}, {"role": "user", "content": "Pode ser quarta."}]
    return asyncio.run(guard_booking_claims(complete, messages, completion, lang="pt", has_booking=has_booking))


def test_an_unbacked_claim_is_written_again_with_a_note(monkeypatch):
    monkeypatch.setattr(get_settings(), "booking_evidence_tools", BOOKING_TOOLS)
    complete, calls = _scripted(Completion(text="Antes de reservar, te passo os valores.", input_tokens=7, cost_usd=0.2))
    result = _guard(Completion(text="Ficou reservado quarta às 10h.", input_tokens=5, cost_usd=0.1), complete)
    assert result.text == "Antes de reservar, te passo os valores."
    assert result.input_tokens == 12 and abs(result.cost_usd - 0.3) < 1e-9
    # The note rides in the system prompt; no message is added mid-conversation.
    assert len(calls) == 1 and len(calls[0]) == 2 and "AVISO DO SISTEMA" in calls[0][0]["content"]


def test_a_claim_made_twice_loses_its_sentences(monkeypatch):
    monkeypatch.setattr(get_settings(), "booking_evidence_tools", BOOKING_TOOLS)
    complete, _ = _scripted(Completion(text="Perfeito, ficou reservado!\n\nQual forma de pagamento prefere?"))
    result = _guard(Completion(text="Ficou reservado quarta."), complete)
    assert result.text == "Qual forma de pagamento prefere?"
    complete, _ = _scripted(Completion(text="Ficou reservado quarta."))
    assert _guard(Completion(text="Ficou reservado quarta."), complete).text.startswith("Me confirma, por favor")


def test_a_backed_claim_goes_out_as_written(monkeypatch):
    monkeypatch.setattr(get_settings(), "booking_evidence_tools", BOOKING_TOOLS)
    complete, calls = _scripted()
    booked = Completion(text="Ficou reservado quarta às 10h.",
                        tool_calls=[{"name": "reservar_horario", "arguments": {}, "result_preview": "ok", "is_error": False}])
    assert _guard(booked, complete).text == "Ficou reservado quarta às 10h."

    async def on_file():
        return True

    earlier = Completion(text="Sua pré-sessão está marcada para segunda.")
    assert _guard(earlier, complete, has_booking=on_file).text == "Sua pré-sessão está marcada para segunda."
    assert calls == []


def test_a_failed_booking_tool_does_not_back_a_claim(monkeypatch):
    monkeypatch.setattr(get_settings(), "booking_evidence_tools", BOOKING_TOOLS)
    complete, calls = _scripted(Completion(text="Não consegui reservar agora."))
    failed = Completion(text="Ficou reservado quarta.",
                        tool_calls=[{"name": "reservar_horario", "arguments": {}, "result_preview": "500", "is_error": True}])
    assert _guard(failed, complete).text == "Não consegui reservar agora." and len(calls) == 1


def test_off_without_evidence_tools(monkeypatch):
    monkeypatch.setattr(get_settings(), "booking_evidence_tools", "")
    complete, calls = _scripted()
    assert _guard(Completion(text="Ficou reservado quarta."), complete).text == "Ficou reservado quarta."
    assert calls == []
