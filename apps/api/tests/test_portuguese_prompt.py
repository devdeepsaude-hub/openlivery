"""Brazilian Portuguese as an agent prompt language.

An agent whose prompt language is "pt" gets every fixed heading and sentence
of its prompt in Portuguese; the operator's own text is inserted as written.
"""

from fastapi.testclient import TestClient

from conftest import TestingSession

from app.models import Agent
from app.services import capture, follow_ups, knowledge, resolution
from app.services.whatsapp_inbound import _media_placeholder


def _agent(client: TestClient, language: str = "pt") -> dict:
    customer = client.post("/api/clients", json={"name": "Clinica Aurora", "is_active": True}).json()
    response = client.post(
        "/api/agents",
        json={
            "client_id": customer["id"], "provider": "openrouter", "model": "google/gemini-3.6-flash",
            "name": "Mariana", "instructions": "Acolha quem procura terapia.", "personality": "Calorosa e direta.",
            "brief_summary": "Clinica de psicologia online.", "brief_donts": "- Nunca prometa resultados.",
            "prompt_language": language,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_an_agent_can_be_set_to_portuguese(authenticated_client: TestClient):
    client = authenticated_client
    agent = _agent(client)
    assert agent["prompt_language"] == "pt"
    updated = client.patch(f"/api/agents/{agent['id']}", json={"prompt_language": "es"})
    assert updated.status_code == 200 and updated.json()["prompt_language"] == "es"
    assert client.patch(f"/api/agents/{agent['id']}", json={"prompt_language": "fr"}).status_code == 422


def test_the_portuguese_prompt_has_no_spanish_scaffolding(authenticated_client: TestClient):
    agent_id = _agent(authenticated_client)["id"]
    with TestingSession() as db:
        prompt = knowledge.build_system_prompt(db.get(Agent, agent_id), "")
    for heading in ("# Mariana, assistente de IA de Clinica Aurora", "## Seu trabalho", "## A empresa", "## Regras", "### Nunca", "## Tom", "## Horários e solicitações"):
        assert heading in prompt, heading
    for spanish in ("Tu trabajo", "El negocio", "Reglas", "Tono", "Horarios y solicitudes", "asistente virtual de la empresa"):
        assert spanish not in prompt, spanish
    # The operator's own rules still follow the base ones.
    assert "você é a assistente virtual da empresa e diz isso se perguntarem" in prompt
    assert prompt.index("Nunca invente") < prompt.index("Nunca prometa resultados")


def test_every_llm_facing_text_resolves_in_portuguese(authenticated_client: TestClient):
    agent_id = _agent(authenticated_client)["id"]
    with TestingSession() as db:
        agent = db.get(Agent, agent_id)
        definitions = capture.field_definitions(db, agent.client_id, "pt")
        assert definitions["name"].label == "Nome" and definitions["email"].label == "E-mail"
        assert resolution.resolution_prompt(agent).startswith("ENCERRAMENTO DA CONVERSA")
    assert follow_ups._TEXT["pt"]["first"].startswith("ACOMPANHAMENTO POR INATIVIDADE")
    assert set(follow_ups._TEXT["pt"]) == set(follow_ups._TEXT["en"])
    assert set(knowledge._PROMPT_TEXT["pt"]) == set(knowledge._PROMPT_TEXT["en"])
    assert set(capture._TEXT["pt"]) == set(capture._TEXT["en"])
    assert knowledge._HUMAN_TURN["pt"]["short"] == "[Escrito por uma pessoa da empresa]"
    assert _media_placeholder("image", "pt") == "[O cliente enviou uma imagem]"
    # Other languages keep the Spanish markers they always had.
    assert _media_placeholder("image", "en") == "[El cliente envió una imagen]"
