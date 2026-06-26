import asyncio
import os
import instructor
from openai import AsyncOpenAI
from contextlib import asynccontextmanager
from typing import Dict, Any
from fastapi import FastAPI, BackgroundTasks, HTTPException, status
import httpx
from dotenv import load_dotenv

from schema import InboundLeadPayload, EnrichmentAnalysis, OutboundEmailDraft

load_dotenv()
state = {}
@asynccontextmanager
async def lifespan(app:FastAPI):
    #Maintains persistent TCP connection poll, making async requests fast
    state["http_client"] = httpx.AsyncClient(timeout=30.0)
    yield
    await state["http_client"].aclose()

app = FastAPI(title="Email Context State Machine", lifespan=lifespan)

ai_client = instructor.from_openai(AsyncOpenAI())
MAX_CONCURRENT_TASKS = 10
rate_limit_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

N8N_HYDRATION_URL = os.getenv("N8N_THREAD_HYDRATION_URL")
N8N_EMAIL_DISPATCHER = os.getenv("N8N_EMAIL_DISPATCH_URL")

if not N8N_HYDRATION_URL or not N8N_EMAIL_DISPATCHER:
    raise RuntimeError("System Boot Error: Missing required n8n webhook environment variables.")

# ASYNC I/O WORKERS (Context Gathering)

async def fetch_context_from_n8n(lead_email: str, crm_id: str) -> Dict[str, Any]:
    """
    Hits a n8n webhook configured to query HubSpot/Gmail/IMAP and return
    recent interaction histories concurrently.
    """
    client: httpx.AsyncClient = state["http_client"]
    try:
        payload = {"email": lead_email, "crm_id": crm_id}
        response = await client.post(N8N_HYDRATION_URL, json=payload)
        response.raise_for_status()
        return response.json()

    except httpx.HTTPError as exc:
        print(f"Error fetching data from n8n pipeline: {exc}")
        return {"threads": [], "crm_notes": "No recent logs found due to I/O error."}

# LLM PIPELINE INTEGRATION

async def generate_lead_profiling(context: Dict[str, Any]) -> EnrichmentAnalysis:
    ai_client: instructor.AsyncInstructor = state["llm_client"]

    return await ai_client.chat.completions.create(
        model="gpt-4o-mini",  # Most efficienct for structured categorization
        response_model=EnrichmentAnalysis,
        temperature=0.1,
        messages=[
            {
                "role": "system",
                "content": "You are an advanced business intelligence data engineer. Evaluate raw data clusters to profile lead attributes."
            },
            {
                "role": "user",
                "content": f"Analyze this full lead data context matrix:\n{context}"
            }
        ],
    )


async def generate_custom_email(context: Dict[str, Any], profile: EnrichmentAnalysis) -> OutboundEmailDraft:
    ai_client: instructor.AsyncInstructor = state["llm_client"]

    lead_profile = context.get("lead_profile", {})
    user_rules = lead_profile.get("custom_instructions")

    rules_block = ""
    if user_rules:
        rules_block = f"\n⚠️ CRITICAL USER-DEFINED INSTRUCTIONS (Adhere to these strictly):\n- {user_rules}\n"

    prompt = f"""
        Write a highly tailored personalized email to this lead. 
        Match their technical profile: {profile.technical_level}.
        Match the operational urgency: {profile.urgency_score}.
        Address their primary pain point directly: {profile.primary_pain_point}.
        {rules_block}
        CRITICAL: Do not use any placeholders or merge tags like [Name] or {{{{company}}}}.
        Speak to them naturally based on the provided history.

        Data Context:
        {context}
        """

    return await ai_client.chat.completions.create(
        model="gpt-4o",  # High-reasoning model for hyper-nuanced copywriting
        response_model=OutboundEmailDraft,
        temperature=0.7,
        messages=[
            {
                "role": "system",
                "content": "You are an elite, contextual enterprise account director. Write natural outreach emails completely free of template hallmarks."
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
    )

# CONCURRENT ORCHESTRATION LAYER (LLM Logic)
async def process_llm_pipeline(lead: InboundLeadPayload, hydration_data: Dict[str, Any]):
    """
    Orchestrates LLM profiling and draft composition safely contained inside
    the asyncio Semaphore queue barrier.
    """
    async with rate_limit_semaphore:
        print(f"Semaphore Slot Acquired. Processing pipeline for {lead.email}")

        context_payload = {
            "lead_profile": lead.model_dump(),
            "hydrated_history": hydration_data
        }
        try:
            profile: EnrichmentAnalysis = await generate_lead_profiling(context_payload)

            generated_draft: OutboundEmailDraft = await generate_custom_email(context_payload, profile)

            client: httpx.AsyncClient = state["http_client"]
            print(f"Outbound validation passed for {lead.email}. Dispatching to n8n outbox.")
            response = await client.post(
                N8N_EMAIL_DISPATCHER,
                json={"recipient": lead.email, "draft": generated_draft.model_dump()}
            )
            response.raise_for_status()

        except Exception as err:
            print(f"Pipeline Execution Failed for {lead.email}: {err}")


# FASTAPI INGESTION GATEWAY
@app.post("/webhook/ingress", status_code=status.HTTP_202_ACCEPTED)
async def handle_webhook_ingress(payload: InboundLeadPayload, background_tasks: BackgroundTasks):
    print(f"Received data payload for lead: {payload.email}")

    hydration_data = await fetch_context_from_n8n(payload.email, payload.crm_contact_id)

    background_tasks.add_task(process_llm_pipeline, payload, hydration_data)

    return {"status": "accepted", "message": "Hydration finished. LLM queue worker engaged."}
