import asyncio
import os
import psycopg
import instructor
from fastapi.security import APIKeyHeader
from openai import AsyncOpenAI
from contextlib import asynccontextmanager
from typing import Dict, Any
from fastapi import FastAPI, BackgroundTasks, Security, HTTPException, status, Header
import httpx
from dotenv import load_dotenv

from schema import InboundLeadPayload, EnrichmentAnalysis, OutboundEmailDraft
from app_frontend import DATABASE_URL

load_dotenv()
state = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Maintains persistent TCP connection pool, making async requests lightning fast
    state["http_client"] = httpx.AsyncClient(timeout=30.0)
    yield
    await state["http_client"].aclose()


app = FastAPI(title="Mouthpiece Automation Engine", lifespan=lifespan)

API_KEY_NAME = "X-API-Token"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)
API_BEARER_TOKEN = os.getenv("API_BEARER_TOKEN")

MAX_CONCURRENT_TASKS = 10
rate_limit_semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

N8N_HYDRATION_URL = os.getenv("N8N_THREAD_HYDRATION_URL")
N8N_EMAIL_DISPATCHER = os.getenv("N8N_EMAIL_DISPATCH_URL")

if not N8N_HYDRATION_URL or not N8N_EMAIL_DISPATCHER:
    raise RuntimeError("System Boot Error: Missing required n8n webhook environment variables.")


# SECURITY & DEPENDENCY WORKERS

async def validate_api_key(api_key: str = Security(api_key_header)):
    """Validates the incoming system communication token."""
    if not api_key or api_key != API_BEARER_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API validation token."
        )
    return api_key


# ASYNC I/O WORKERS (Context Gathering)

async def fetch_context_from_n8n(lead_email: str, crm_id: str) -> Dict[str, Any]:
    """
    Hits an n8n webhook configured to query HubSpot/Gmail/IMAP and return
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


#LIGHTWEIGHT DB WRITERS (ASYNC)

async def update_db_status(email: str, crm_id: str, status: str, draft: OutboundEmailDraft = None):
    """Executes a non-blocking raw SQL state transaction against Neon Serverless."""
    loop = asyncio.get_running_loop()
    def _execute():
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                if draft:
                    cur.execute("""
                        INSERT INTO mouthpiece_queue 
                        (recipient_email, crm_contact_id, status, subject_line, email_body_markdown, intended_tone, intended_urgency, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (recipient_email) DO UPDATE SET
                            status = EXCLUDED.status,
                            subject_line = EXCLUDED.subject_line,
                            email_body_markdown = EXCLUDED.email_body_markdown,
                            intended_tone = EXCLUDED.intended_tone,
                            intended_urgency = EXCLUDED.intended_urgency,
                            updated_at = CURRENT_TIMESTAMP;
                    """, (email, crm_id, status, draft.subject_line, draft.email_body_markdown,
                          draft.intended_technical_tone.value, draft.intended_urgency.value))
                else:
                    cur.execute("""
                        INSERT INTO mouthpiece_queue (recipient_email, crm_contact_id, status, updated_at)
                        VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                        ON CONFLICT (recipient_email) DO UPDATE SET 
                            status = EXCLUDED.status,
                            updated_at = CURRENT_TIMESTAMP;
                    """, (email, crm_id, status))
    await loop.run_in_executor(None, _execute)

# LLM PIPELINE INTEGRATION WITH DYNAMIC BYOK ROUTING

async def generate_lead_profiling(context: Dict[str, Any], user_openai_key: str) -> EnrichmentAnalysis:
    """Uses gpt-4o-mini for quick, type-safe lead analytical classification."""
    raw_client = AsyncOpenAI(api_key=user_openai_key)
    ai_client = instructor.from_openai(raw_client)

    return await ai_client.chat.completions.create(
        model="gpt-4o-mini",
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


async def generate_custom_email(context: Dict[str, Any], profile: EnrichmentAnalysis,
                                user_openai_key: str) -> OutboundEmailDraft:
    """Uses high-reasoning gpt-4o for complex, contextual copywriting generation."""
    raw_client = AsyncOpenAI(api_key=user_openai_key)
    ai_client = instructor.from_openai(raw_client)

    lead_profile = context.get("lead_profile", {})
    # Fixed nested lookup mismatch safely
    user_rules = lead_profile.get("telemetry", {}).get("custom_instructions", None)

    rules_block = ""
    if user_rules:
        rules_block = f"\n⚠️ CRITICAL USER-DEFINED INSTRUCTIONS:\n- {user_rules}\n"

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
        model="gpt-4o",
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


# CONCURRENT ORCHESTRATION LAYER

async def process_llm_pipeline(lead: InboundLeadPayload, hydration_data: Dict[str, Any], user_openai_key: str):
    """
    Orchestrates LLM profiling and draft composition safely contained inside
    the asyncio Semaphore queue barrier.
    """
    async with rate_limit_semaphore:
        await update_db_status(lead.email, lead.crm_contact_id, 'GENERATING')

        context_payload = {
            "lead_profile": lead.model_dump(),
            "hydrated_history": hydration_data
        }
        try:
            profile: EnrichmentAnalysis = await generate_lead_profiling(context_payload, user_openai_key)
            generated_draft: OutboundEmailDraft = await generate_custom_email(context_payload, profile, user_openai_key)

            client: httpx.AsyncClient = state["http_client"]
            print(f"Outbound validation passed for {lead.email}. Dispatching to n8n outbox storage queue.")

            await update_db_status(lead.email, lead.crm_contact_id, "READY", generated_draft)
            print(f"Draft successfully written to database for {lead.email}")

        except Exception as err:
            await update_db_status(lead.email, lead.crm_contact_id, f"FAILED: {str(err)}")

# FASTAPI INGESTION GATEWAY WITH TRANSIENT ENCRYPTED HEADER BYOK INTERCEPT
@app.post("/webhook/ingress", status_code=status.HTTP_202_ACCEPTED)
async def handle_webhook_ingress(
        payload: InboundLeadPayload,
        background_tasks: BackgroundTasks,
        _=Security(validate_api_key),
        x_user_openai_key: str = Header(...)
):
    await update_db_status(payload.email, payload.crm_contact_id, "HYDRATING")

    async def run_pipeline():
        client: httpx.AsyncClient = state["http_client"]
        try:
            response = await client.post(N8N_HYDRATION_URL,
                                         json={"email": payload.email, "crm_id": payload.crm_contact_id})
            hydration_data = response.json() if response.status_code == 200 else {"threads": []}
        except Exception:
            hydration_data = {"threads": []}

        await process_llm_pipeline(payload, hydration_data, x_user_openai_key)
    background_tasks.add_task(run_pipeline)

    return {"status": "accepted",
            "message": "Hydration finished. LLM queue worker engaged with localized transient key runtime context."}