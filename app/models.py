from typing import Any, Dict, Literal, Optional
from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    question: str = Field(
        ...,
        description="The user question or command to process through the AI workflow.",
        examples=["Create a P1 incident ticket for high latency in the payment service"],
    )
    simulate_mode: Optional[Literal["normal", "llm_timeout", "db_outage"]] = Field(
        default="normal",
        description="Optional chaos flag to deterministically test fallback and error observability.",
    )


class BusinessActionResult(BaseModel):
    action_type: str
    status: Literal["executed", "skipped", "failed"]
    summary: str
    payload: Dict[str, Any]
    execution_time_ms: float


class TokenUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class AskResponse(BaseModel):
    answer: str
    intent: str
    model_used: str
    fallback_triggered: bool
    business_action: BusinessActionResult
    token_usage: TokenUsage
    latency_ms: float
    request_id: str
    trace_id: str


class ErrorResponse(BaseModel):
    error_code: str
    message: str
    details: Optional[str] = None
    request_id: str
    trace_id: str


class HealthResponse(BaseModel):
    status: Literal["healthy", "degraded"]
    service: str
    version: str
    uptime_seconds: float
    checks: Dict[str, str]