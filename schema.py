from datetime import datetime
from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field, EmailStr, field_validator


class UrgencyLevel(str, Enum):
    URGENT = "urgent"
    NORMAL = "normal"
    LAID_BACK = "laid_back"

class TechnicalSophistication(str, Enum):
    LEADERSHIP = "executive_leadership"
    MID_LEVEL = "functional_user"
    HARDCORE = "deep_technical"

class LifecycleStage(str, Enum):
    NEW_LEAD = "new_lead"
    ACTIVE_TRIAL = "active_trial"
    CHURN_RISK = "churn_risk"
    EXPANSION_TARGET = "expansion_target"


# INBOUND DATA MODELS (Ingestion & Hydration)

class TelemetryEvent(BaseModel):
    event_name: str = Field(..., examples=["webhook_configured", "api_rate_limit_hit"])
    timestamp: datetime
    metadata: dict = Field(default_factory=dict, description="Arbitrary KV pairs from system logs")

class ProductTelemetrySummary(BaseModel):
    days_active_last_30: int = Field(..., ge=0, le=30)
    features_used: List[str] = Field(default_factory=list)
    recent_critical_events: List[TelemetryEvent] = Field(default_factory=list)
    monthly_active_score: float = Field(..., description="0.0 to 1.0 indicator of user engagement health")

class HistoricalEmailThread(BaseModel):
    thread_id: str
    last_interaction_timestamp: datetime
    direction: str = Field(..., description="'inbound' or 'outbound'")
    snippet: str = Field(..., description="The highly condensed content or summary of the last 2-3 turns")

class InboundLeadPayload(BaseModel):
    """
    The unified incoming payload received by FastAPI, fully hydrated by n8n/CRM data.
    """
    email: EmailStr
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company_name: str
    crm_contact_id: str
    lifecycle_stage: LifecycleStage
    telemetry: ProductTelemetrySummary
    recent_threads: List[HistoricalEmailThread] = Field(
        default_factory=list,
        description="Hydrated recent interaction history to contextually align the copy"
    )

# INTERMEDIARY ANALYSIS MODELS


class EnrichmentAnalysis(BaseModel):
    """
    Output model for separate profiling pass before the email generation pass.
    """
    technical_level: TechnicalSophistication
    urgency_score: UrgencyLevel
    primary_pain_point: str = Field(..., description="The deduced core issue the client faces based on thread history + telemetry")
    suggested_angle: str = Field(..., description="Strategic direction for the copy (e.g., 'Acknowledge API friction, offer direct technical fix')")



# OUTBOUND DATA MODELS (LLM Generation)

class OutboundEmailDraft(BaseModel):
    """
    The rigid schema forced onto the LLM output to guarantee structural alignment
    and kill off the merge-tag 'uncanny valley'.
    """
    subject_line: str = Field(
        ...,
        description="Hyper-personalized subject line reflecting actual context. ABSOLUTELY NO generic merge tags."
    )
    email_body_markdown: str = Field(
        ...,
        description="The actual body of the email. Must address the recipient naturally using their details. Avoid template speak."
    )
    intended_technical_tone: TechnicalSophistication = Field(
        ...,
        description="Self-asserted confirmation by the LLM of the tone profile used in this draft."
    )
    intended_urgency: UrgencyLevel = Field(
        ...,
        description="Self-asserted confirmation of the operational speed/tone profile utilized."
    )
    safety_checksum_passed: bool = Field(
        ...,
        description="Set to true only if you have programmatically verified that no placeholder bracket notations (e.g., [Name], {{company}}) are anywhere in the draft."
    )

    @field_validator("email_body_markdown", "subject_line")
    @classmethod
    def check_for_uncanny_placeholders(cls, value: str) -> str:
        """
        Pydantic layer barrier preventing standard engine placeholders from leaking to production.
        """
        banned_patterns = ["{{", "}}", "[", "]", "merge_tag", "insert here", "dear input"]
        for pattern in banned_patterns:
            if pattern in value:
                raise ValueError(f"Draft validation failed: Found prohibited placeholder pattern '{pattern}' inside text.")
        return value


# EXAMPLE USAGE & CONTEXT VALIDATION
if __name__ == "__main__":
    # Mocking data coming through FastAPI / n8n
    sample_inbound_json = {
        "email": "dev-ops-lead@scaleup.io",
        "first_name": "Alex",
        "company_name": "ScaleUp",
        "crm_contact_id": "contact_992811",
        "lifecycle_stage": "active_trial",
        "telemetry": {
            "days_active_last_30": 12,
            "features_used": ["api_v2", "billing_dashboard"],
            "monthly_active_score": 0.85,
            "recent_critical_events": [
                {
                    "event_name": "api_rate_limit_hit",
                    "timestamp": "2026-06-25T11:20:00Z",
                    "metadata": {"endpoint": "/v2/records", "limit_threshold": 5000}
                }
            ]
        },
        "recent_threads": [
            {
                "thread_id": "thread_abc123",
                "last_interaction_timestamp": "2026-06-24T15:30:00Z",
                "direction": "inbound",
                "snippet": "Alex asked why their bulk upsert payload was yielding 429 errors despite staying within daily limits."
            }
        ]
    }

    # Validate incoming payload
    validated_inbound = InboundLeadPayload(**sample_inbound_json)
    print(" Inbound Payload Correctly Validated Structure.")
    print(f"Analyzing data for: {validated_inbound.first_name} at {validated_inbound.company_name}")
    print(f"Critical telemetry points to ingest: {validated_inbound.telemetry.recent_critical_events[0].event_name}\n")

    # Mocking a successfully built structured output from the LLM pipeline
    sample_llm_outbound = {
        "subject_line": "Resolving your bulk upsert rate limits on /v2/records",
        "email_body_markdown": "Hi Alex,\n\nI saw your team hit a wall with our rate limiter on `/v2/records` yesterday morning. Your script was pushing 5k records concurrently, which triggers our concurrency wall rather than the daily volume budget.\n\nTo unblock ScaleUp, you'll want to chunk those into pools of 500 or introduce a simple leaky-bucket strategy on your async queue. Let me know if you want me to drop an example repository here.\n\nBest,\nYour Integration Team",
        "intended_technical_tone": "deep_technical",
        "intended_urgency": "urgent",
        "safety_checksum_passed": True
    }

    # Validate outgoing response payload
    validated_outbound = OutboundEmailDraft(**sample_llm_outbound)
    print(" Outbound Email Schema Cleared For Dispatch Queue.")
    print(f"Subject Line: {validated_outbound.subject_line}")