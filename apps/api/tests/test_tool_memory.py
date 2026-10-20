"""Earlier tool results travel in the history, so the agent remembers what it
looked up or did (a booking it made) instead of redoing or contradicting it."""

from types import SimpleNamespace

from app.services.knowledge import llm_turns


def _message(role, content, tool_calls=None, sender_type="ai"):
    return SimpleNamespace(role=role, content=content, llm_content=None, sender_type=sender_type, tool_calls=tool_calls)


def test_an_earlier_reply_carries_what_its_tools_returned():
    booking = [{
        "name": "reservar_horario",
        "arguments": {"psicologa_id": "jessica", "data": "2026-10-22", "hora": "18:00"},
        "result_preview": 'HTTP 201: {"reserva_id": "R8E0A09", "status": "aguardando_pagamento"}',
        "is_error": False,
    }]
    turns = llm_turns([
        _message("user", "pode reservar"),
        _message("assistant", "Pronto, reservei.", booking),
        _message("user", "e agora?"),
    ], "pt")
    reply = turns[1]["content"]
    assert reply.startswith("[Nota interna, não repita para o cliente")
    assert "reservar_horario(psicologa_id=jessica, data=2026-10-22, hora=18:00)" in reply
    assert "R8E0A09" in reply and reply.endswith("Pronto, reservei.")
    # Customer turns and replies without tools are left as they were.
    assert turns[0]["content"] == "pode reservar" and turns[2]["content"] == "e agora?"


def test_a_failed_tool_is_marked_and_long_results_are_cut():
    calls = [{"name": "gerar_pix", "arguments": {}, "result_preview": "x" * 1000, "is_error": True}]
    reply = llm_turns([_message("assistant", "Não consegui.", calls)], "en")[0]["content"]
    assert "gerar_pix() (failed) -> " in reply and "x" * 301 not in reply
