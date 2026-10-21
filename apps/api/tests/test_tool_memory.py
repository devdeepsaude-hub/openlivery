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


def test_the_note_carries_as_much_as_set_and_marks_what_it_cut(monkeypatch):
    from app.config import get_settings
    from app.services.tools import loop

    monkeypatch.setattr(get_settings(), "tool_note_result_chars", 2000)
    # The stored preview keeps at least what the note will carry.
    metadata = []
    loop._record(metadata, "listar_psicologas", {}, "z" * 3000, False)
    assert metadata[0]["result_preview"] == "z" * 2000 + "…"

    whole = [{"name": "listar_psicologas", "arguments": {}, "result_preview": "y" * 1500, "is_error": False}]
    reply = llm_turns([_message("assistant", "Indico a Karine.", whole)], "pt")[0]["content"]
    assert "y" * 1500 in reply and "[cortado nesta nota]" not in reply
    # A preview already cut when stored is marked too, so the agent knows the
    # rest existed instead of taking what it said about it as made up.
    cut = [{"name": "listar_psicologas", "arguments": {}, "result_preview": "y" * 1500 + "…", "is_error": False}]
    reply = llm_turns([_message("assistant", "Indico a Karine.", cut)], "pt")[0]["content"]
    assert "y" * 1500 + "… [cortado nesta nota]" in reply and "você viu os resultados inteiros" in reply.lower()
