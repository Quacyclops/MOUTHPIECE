import pytest
from fastapi.testclient import TestClient
import httpx
import respx
from unittest.mock import AsyncMock, patch

from app import app, state
from schema import EnrichmentAnalysis, OutboundEmailDraft

# Configuring pytest to automatically handle async test markers
pytestmark = pytest.mark.asyncio


@pytest.fixture
def client():
    with TestClient(app) as tc:
        yield tc


@pytest.fixture(autouse=True)
async def mock_lifespan_state():
    state["http_client"] = httpx.AsyncClient(timeout=1.0)
    state["llm_client"] = AsyncMock()
    yield
    await state["http_client"].aclose()


# THE TEST CASES

@respx.mock  # Automatically intercepts outbound HTTP calls made via httpx
async def test_webhook_ingress_triggers_hydration_and_accepts(client):
    """Verifies the ingress gateway successfully pulls data from n8n and returns a 202."""

    # Mock the outbound hydration webhook call to n8n
    n8n_hydration_route = respx.post("https://mock-n8n-url.com/hydration").mock(
        return_value=httpx.Response(200, json={"threads": ["Past email 1"], "crm_notes": "Good lead"})
    )

    dummy_payload = {
        "email": "test@enterprise.com",
        "crm_contact_id": "crm_12345",
        "custom_instructions": "Keep it concise"
    }

    headers = {"X-API-Token": "your_ultra_secure_secret_token_here"}

    with patch("os.getenv", side_effect=lambda k: {
        "N8N_THREAD_HYDRATION_URL": "https://mock-n8n-url.com/hydration",
        "N8N_EMAIL_DISPATCH_URL": "https://mock-n8n-url.com/dispatch",
        "API_BEARER_TOKEN": "your_ultra_secure_secret_token_here"
    }.get(k)):
        response = client.post("/webhook/ingress", json=dummy_payload)

    # Assertions
    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert n8n_hydration_route.called


@patch("app.generate_lead_profiling")
@patch("app.generate_custom_email")
@respx.mock
async def test_process_llm_pipeline_success(mock_email_gen, mock_profile_gen, client):
    """Tests the concurrency worker execution loop from profile to outbox delivery."""

    mock_profile_gen.return_value = EnrichmentAnalysis(
        technical_level="Senior Developer",
        urgency_score="High",
        primary_pain_point="Database locks"
    )

    mock_email_gen.return_value = OutboundEmailDraft(
        subject="Optimizing database structures",
        body="Hey, noticed your database locks..."
    )

    # Intercept the outbound dispatch to the n8n outbox
    n8n_dispatch_route = respx.post("https://mock-n8n-url.com/dispatch").mock(
        return_value=httpx.Response(200, json={"status": "sent"})
    )

    # Import the target background pipeline directly to execute manually
    from app import process_llm_pipeline, InboundLeadPayload

    test_lead = InboundLeadPayload(
        email="dev@studio.com",
        crm_contact_id="1122",
        custom_instructions="None"
    )
    test_hydration = {"threads": [], "crm_notes": "Fresh lead"}

    with patch("os.getenv", return_value="https://mock-n8n-url.com/dispatch"):
        # Manually invoke the background worker pipeline task
        await process_llm_pipeline(test_lead, test_hydration)

    # Assertions: Did it hit the outbox route with the correctly structured model attributes?
    assert n8n_dispatch_route.called
    last_request_payload = n8n_dispatch_route.calls.last.request.read().decode()
    assert "dev@studio.com" in last_request_payload
    assert "Optimizing database structures" in last_request_payload