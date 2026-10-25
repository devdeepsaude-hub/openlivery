from datetime import datetime, timezone
from types import SimpleNamespace

from app.config import get_settings
from app.services.reply_names import drop_name, first_name, keep_name_to_greeting


def test_first_name_skips_symbols_and_titles():
    assert first_name("🌻 Carla Souza") == "Carla"
    assert first_name("Dra. Paula Lima") == "Paula"
    assert first_name("Marcus Vinicius") == "Marcus"
    assert first_name("") == ""
    assert first_name(None) == ""
    assert first_name("J") == ""


def test_name_after_a_comma_is_dropped_and_punctuation_kept():
    assert drop_name("Legal, Carlos.\n\nVocê já fez terapia?", "Carlos") == "Legal.\n\nVocê já fez terapia?"
    assert drop_name("Poxa, Carlos, sinto muito.", "Carlos") == "Poxa, sinto muito."
    assert drop_name("Que bom que você decidiu cuidar disso, Ana.", "Ana") == "Que bom que você decidiu cuidar disso."
    assert drop_name("Oi, Ana! Que bom te ver de novo.", "Ana") == "Oi! Que bom te ver de novo."
    assert drop_name("Entendi, ana", "Ana") == "Entendi"


def test_name_opening_a_sentence_is_dropped_and_the_sentence_recapitalized():
    assert drop_name("Ana, me conta: o que te trouxe?", "Ana") == "Me conta: o que te trouxe?"
    assert drop_name("Entendi. Ana, você prefere manhã ou noite?", "Ana") == "Entendi. Você prefere manhã ou noite?"
    assert drop_name("Faz sentido.\nAna, quer marcar?", "Ana") == "Faz sentido.\nQuer marcar?"


def test_the_name_stays_where_it_does_not_address_the_reader():
    # A team member who shares the contact's name.
    assert drop_name("À noite tenho a Juliana, da psicanálise.", "Juliana") == "À noite tenho a Juliana, da psicanálise."
    assert drop_name("Tenho horários com a Karine, Juliana ou Andrezza.", "Juliana") == (
        "Tenho horários com a Karine, Juliana ou Andrezza."
    )
    # Part of another word.
    assert drop_name("Anabela, que nome bonito.", "Ana") == "Anabela, que nome bonito."


def test_nothing_to_match_leaves_the_text_alone():
    assert drop_name("Legal, Carlos.", "") == "Legal, Carlos."
    assert drop_name("", "Carlos") == ""


def _conversation(answered: bool, name: str = "Carlos Souza"):
    return SimpleNamespace(
        contact_name=name,
        first_reply_at=datetime(2026, 10, 25, tzinfo=timezone.utc) if answered else None,
    )


def test_the_greeting_keeps_the_name_and_later_replies_do_not(monkeypatch):
    monkeypatch.setattr(get_settings(), "reply_name_only_in_greeting", True)
    assert keep_name_to_greeting("Oi, Carlos! Sou a Mariana.", _conversation(answered=False)) == "Oi, Carlos! Sou a Mariana."
    assert keep_name_to_greeting("Legal, Carlos. E você?", _conversation(answered=True)) == "Legal. E você?"


def test_off_by_default(monkeypatch):
    monkeypatch.setattr(get_settings(), "reply_name_only_in_greeting", False)
    assert keep_name_to_greeting("Legal, Carlos.", _conversation(answered=True)) == "Legal, Carlos."
