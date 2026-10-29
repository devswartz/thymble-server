from fastapi.testclient import TestClient

from tests.conftest import make_settings
from thymble.main import create_app


def capabilities(**settings: object) -> dict:
    client = TestClient(create_app(make_settings(**settings)))
    response = client.get("/v1/capabilities")
    assert response.status_code == 200
    return response.json()


def test_defaults_report_nothing_optional():
    body = capabilities()
    assert body["api_version"] == "1.0"
    assert body["llm"] is False
    assert body["photo_import"] is False
    assert body["email_sign_in"] is False
    assert body["connectors"] == {"lists": [], "retailers": []}
    assert body["shared_learning"] is False


def test_anthropic_key_enables_llm_and_photo_import():
    body = capabilities(anthropic_api_key="sk-test-placeholder")
    assert body["llm"] is True
    assert body["photo_import"] is True


def test_local_endpoint_enables_llm():
    body = capabilities(openai_compatible_base_url="http://ollama:11434/v1")
    assert body["llm"] is True


def test_photo_import_can_be_switched_off():
    body = capabilities(anthropic_api_key="sk-test-placeholder", photo_import_enabled=False)
    assert body["llm"] is True
    assert body["photo_import"] is False


def test_smtp_enables_email_sign_in():
    body = capabilities(smtp_host="smtp.example.com", smtp_from="thymble@example.com")
    assert body["email_sign_in"] is True


def test_connectors_from_comma_separated_env(monkeypatch):
    monkeypatch.setenv("CONNECTORS_LISTS", "home_assistant")
    monkeypatch.setenv("CONNECTORS_RETAILERS", "walmart, kroger")
    monkeypatch.setenv("SHARED_LEARNING", "true")
    body = capabilities()
    assert body["connectors"] == {"lists": ["home_assistant"], "retailers": ["walmart", "kroger"]}
    assert body["shared_learning"] is True
