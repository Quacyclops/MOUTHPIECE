# Mouthpiece: Contextual Email Ingestion & Generation Engine

Mouthpiece is an asynchronous backend service built to ingest inbound leads, fetch their historical communication records, and generate tailored outbound email drafts using structured LLM outputs.

## System Architecture

The application is structured as an event-driven state machine to process data efficiently without locking up system resources:

1. **Ingress Gate (`/webhook/ingress`)**: A FastAPI endpoint accepts inbound lead payloads, validates them via Pydantic, and handles initial data collection.
2. **Context Hydration**: The system queries an external n8n webhook to retrieve past interaction histories and CRM records matching the lead.
3. **Throttled Background Worker Pool**: Heavy LLM processing is offloaded to a background queue protected by an asyncio semaphore limit (maximum 10 concurrent operations) to prevent rate-limiting or server fatigue.
4. **Two-Pass Generation**:
   - **Pass 1 (Analysis)**: Extracted data is evaluated by a fast model (`gpt-4o-mini`) via the `instructor` library to output a structured lead profile.
   - **Pass 2 (Composition)**: A high-reasoning model (`gpt-4o`) reads the profile, checks for specific user-defined rules, and generates a natural email draft.
5. **Dispatch**: The finalized draft is validated against a strict schema and posted directly back to an n8n outbound mail gateway.

## Environment Configuration

Create a `.env` file in the root directory with the following variables:

```env
N8N_THREAD_HYDRATION_URL="https://your-n8n-instance/hydration-endpoint"
N8N_EMAIL_DISPATCH_URL="https://your-n8n-instance/dispatch-endpoint"
API_BEARER_TOKEN="your_secure_api_validation_token"
OPENAI_API_KEY="your_openai_api_key"