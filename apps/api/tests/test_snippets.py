"""Fixed messages: a block in the agent's texts goes out word for word where
the reply names it."""

from types import SimpleNamespace

from fastapi.testclient import TestClient

from conftest import TestingSession

from app.models import Agent
from app.services import knowledge
from app.services.snippets import blocks, expand

PRICES = "Os valores são os seguintes:\n\n✨ Pagamento semanal: R$ 150 por sessão\n\n_Pagamento via boleto ou cartão._"


def _agent(**texts):
    fields = {name: "" for name in ("instructions", "personality", "brief_summary", "brief_products",
                                    "brief_audience", "brief_policies", "brief_dos", "brief_donts")}
    return SimpleNamespace(**{**fields, **texts})


def test_blocks_are_read_from_any_agent_text():
    agent = _agent(brief_products=f"Terapia online.\n[[valores]]\n{PRICES}\n[[/valores]]\nMais nada.",
                   instructions="[[Boas_vindas]]\nOi!\n[[/boas_vindas]]")
    assert blocks(agent) == {"valores": PRICES, "boas_vindas": "Oi!"}


def test_a_marker_becomes_the_whole_block_as_its_own_paragraphs():
    found = {"valores": PRICES}
    assert expand("Te passo os valores:\n[[valores]]\nQual faz mais sentido?", found) == (
        f"Te passo os valores:\n\n{PRICES}\n\nQual faz mais sentido?"
    )
    assert expand("[[valores]]", found) == PRICES


def test_unknown_and_closing_markers_are_dropped():
    assert expand("Oi [[nada]] tudo bem?[[/valores]]", {"valores": PRICES}) == "Oi tudo bem?"


def test_text_without_markers_is_untouched():
    assert expand("Sem marcador, *negrito* e _itálico_.", {"valores": PRICES}) == "Sem marcador, *negrito* e _itálico_."
    assert expand("", {"valores": PRICES}) == ""


def test_the_prompt_lists_the_fixed_messages_and_how_to_send_them(authenticated_client: TestClient):
    client = authenticated_client
    customer = client.post("/api/clients", json={"name": "Clinica Aurora", "is_active": True}).json()
    agent = client.post("/api/agents", json={
        "client_id": customer["id"], "provider": "openrouter", "model": "google/gemini-3.6-flash", "name": "Mariana",
        "prompt_language": "pt", "brief_products": f"Terapia online.\n[[valores]]\n{PRICES}\n[[/valores]]",
    }).json()
    with TestingSession() as db:
        prompt = knowledge.build_system_prompt(db.get(Agent, agent["id"]), "")
    assert "## Mensagens fixas" in prompt and "- [[valores]]" in prompt
    # The block itself stays in the prompt, so the agent knows what it says.
    assert PRICES in prompt

    plain = client.post("/api/agents", json={
        "client_id": customer["id"], "provider": "openrouter", "model": "google/gemini-3.6-flash", "name": "Ana",
        "prompt_language": "pt",
    }).json()
    with TestingSession() as db:
        assert "Mensagens fixas" not in knowledge.build_system_prompt(db.get(Agent, plain["id"]), "")
