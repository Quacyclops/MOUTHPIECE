import asyncio
import os
from typing import Dict, Any
from fastapi import FastAPI, BackgroundTasks, HTTPException, status
import httpx
from dotenv import load_dotenv

from schema import InboundLeadPayload, EnrichmentAnalysis, OutboundEmailDraft

load_dotenv()

app = FastAPI(title="Email Context State Machine")

# SYSTEM CORES & CONFIGURATION
MAX_CONCURRENT_TASKS = 10
rate_limit_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

N8N_HYDRATION_URL = os.getenv("8N_THREAD_HYDRATION_URL")
N8N_EMAIL_DISPATCHER = os.getenv("N8N_EMAIL_DISPATCH_URL")

if not N8N_HYDRATION_URL or not N8N_EMAIL_DISPATCHER:
    raise RuntimeError("System Boot Error: Missing required n8n webhook environment variables.")

# ASYNC I/O WORKERS (Context Gathering)

async def fetch_context_from_n8n(lead_email: str, crm_id: str) -> Dict[str, Any]:
    """
    Hits a n8n webhook configured to query HubSpot/Gmail/IMAP and return
    recent interaction histories concurrently.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            payload = {"email": lead_email, "crm_id": crm_id}
            response = await client.post(N8N_HYDRATION_URL, json=payload)
            response.raise_for_status()
            return response.json()

        except httpx.HTTPError as exc:
            print(f"Error fetching data from n8n pipeline: {exc}")
            return {"threads": [], "crm_notes": "No recent logs found due to I/O error."}


# CONCURRENT ORCHESTRATION LAYER (LLM Logic)
async def process_llm_pipeline(lead: InboundLeadPayload, hydration_data: Dict[str, Any]):
    """
    Orchestrates LLM profiling and draft composition safely contained inside
    the asyncio Semaphore queue barrier.
    """
    async with rate_limit_semaphore:
        print(f"🚦 Semaphore Slot Acquired. Processing pipeline for {lead.email}")

        context_payload = {
            "lead_profile": lead.model_dump(),
            "hydrated_history": hydration_data
        }

        # NOTE: Connect your actual Instructor or Gemini client invocation wrapper here
        # targeting the EnrichmentAnalysis and OutboundEmailDraft structural objects.
        try:
            profile: EnrichmentAnalysis = await generate_lead_profiling(context_payload)

            generated_draft: OutboundEmailDraft = await generate_custom_email(context_payload, profile)

            # 3. Dispatch the validated, tag-clean payload to the n8n outbox gateway
            async with httpx.AsyncClient(timeout=30.0) as client:
                print(f"Outbound validation passed for {lead.email}. Dispatching to n8n outbox.")
                response = await client.post(
                    N8N_EMAIL_DISPATCHER,
                    json={"recipient": lead.email, "draft": generated_draft.model_dump()}
                )
                response.raise_for_status()

        except Exception as err:
            print(f"Pipeline Execution Failed for {lead.email}: {err}")

# PLACEHOLDER WRAPPERS FOR LLM CALLS

async def generate_lead_profiling(context: Dict[str, Any]) -> EnrichmentAnalysis:
    # your_llm_client.chat.completions.create(response_model=EnrichmentAnalysis, ...)
    pass


async def generate_custom_email(context: Dict[str, Any], profile: EnrichmentAnalysis) -> OutboundEmailDraft:
    # your_llm_client.chat.completions.create(response_model=OutboundEmailDraft, ...)
    pass



# FASTAPI INGESTION GATEWAY
@app.post("/webhook/ingress", status_code=status.HTTP_200_OK)
async def handle_webhook_ingress(payload: InboundLeadPayload, background_tasks: BackgroundTasks):
    """
    Validates inbound structure instantly, triggers async context hydration,
    and returns a fast status tracking callback before spinning up LLM workers.
    """
    print(f"Received data payload for lead: {payload.email}")

    hydration_data = await fetch_context_from_n8n(payload.email, payload.crm_contact_id)

    background_tasks.add_task(process_llm_pipeline, payload, hydration_data)

    return {"status": "accepted", "message": "Hydration finished. LLM queue worker engaged."}
