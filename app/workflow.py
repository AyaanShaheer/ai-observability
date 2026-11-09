import asyncio
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Tuple

from opentelemetry.trace import Status, StatusCode

from app.models import AskRequest, AskResponse, BusinessActionResult, TokenUsage
from app.observability import (
    AI_STAGE_LATENCY_SECONDS,
    AI_WORKFLOW_REQUESTS_TOTAL,
    LLM_FALLBACK_TRIGGERS_TOTAL,
    LLM_TOKENS_TOTAL,
    get_current_trace_context,
    get_request_id,
    logger,
    tracer,
)


# ---------------------------------------------------------------------------
# Custom Exceptions for Observability & Troubleshooting Demos
# ---------------------------------------------------------------------------
class GuardrailValidationError(Exception):
    def __init__(self, message: str, reason: str):
        super().__init__(message)
        self.message = message
        self.reason = reason


class UpstreamDatastoreError(Exception):
    def __init__(self, message: str, dependency: str):
        super().__init__(message)
        self.message = message
        self.dependency = dependency


# ---------------------------------------------------------------------------
# Mock Production Datastore (Supports Business Actions)
# ---------------------------------------------------------------------------
MOCK_SERVICE_METRICS: Dict[str, Dict[str, Any]] = {
    "payment-gateway": {"p95_latency_ms": 480, "error_rate_pct": 2.4, "sla_status": "BREACHED", "uptime_pct": 99.12},
    "auth-service": {"p95_latency_ms": 42, "error_rate_pct": 0.05, "sla_status": "HEALTHY", "uptime_pct": 99.99},
    "checkout-worker": {"p95_latency_ms": 195, "error_rate_pct": 0.30, "sla_status": "HEALTHY", "uptime_pct": 99.95},
}

INCIDENT_TICKETS_DB: list[Dict[str, Any]] = []


@contextmanager
def observe_stage(stage_name: str, attributes: Dict[str, Any] | None = None):
    """Context manager that wraps a pipeline stage in both an OTel Span and a Prometheus Histogram."""
    start = time.perf_counter()
    with tracer.start_as_current_span(stage_name) as span:
        if attributes:
            for k, v in attributes.items():
                span.set_attribute(k, v)
        try:
            yield span
            span.set_status(Status(StatusCode.OK))
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise
        finally:
            elapsed = time.perf_counter() - start
            AI_STAGE_LATENCY_SECONDS.labels(stage=stage_name).observe(elapsed)


# ---------------------------------------------------------------------------
# Stage 1: Input Guardrails & Validation
# ---------------------------------------------------------------------------
async def validate_question_guardrails(req: AskRequest) -> str:
    with observe_stage("guardrails.validate_input") as span:
        cleaned = req.question.strip()
        span.set_attribute("input.length", len(cleaned))

        if not cleaned:
            logger.warning(
                "Input rejected by guardrails: empty question",
                extra={"event": "guardrail_rejected", "reason": "empty_question"},
            )
            raise GuardrailValidationError(
                message="Question cannot be empty or whitespace only.",
                reason="empty_question",
            )

        if len(cleaned) < 4:
            logger.warning(
                "Input rejected by guardrails: question too short",
                extra={"event": "guardrail_rejected", "reason": "question_too_short", "length": len(cleaned)},
            )
            raise GuardrailValidationError(
                message="Question must be at least 4 characters long.",
                reason="question_too_short",
            )

        lowered = cleaned.lower()
        forbidden_patterns = ["drop table", "ignore previous instructions", "<script>"]
        for pattern in forbidden_patterns:
            if pattern in lowered:
                logger.error(
                    "Security guardrail triggered on malicious input pattern",
                    extra={"event": "security_guardrail_blocked", "matched_pattern": pattern},
                )
                raise GuardrailValidationError(
                    message=f"Input blocked by security policy (matched forbidden pattern: '{pattern}').",
                    reason="prompt_injection_or_sqli",
                )

        return cleaned


# ---------------------------------------------------------------------------
# Stage 2: Intent Classification Router
# ---------------------------------------------------------------------------
async def classify_intent(question: str) -> Tuple[str, str]:
    with observe_stage("router.classify_intent") as span:
        await asyncio.sleep(0.015)  # Simulate lightweight classifier latency (15ms)
        q_lower = question.lower()

        if any(kw in q_lower for kw in ["ticket", "incident", "p1", "p2", "outage", "escalate", "page"]):
            intent, action = "incident_management", "create_incident_ticket"
        elif any(kw in q_lower for kw in ["report", "sla", "metrics", "status", "latency", "health"]):
            intent, action = "sla_reporting", "generate_sla_report"
        else:
            intent, action = "general_qa", "query_knowledge_base"

        span.set_attribute("router.intent", intent)
        span.set_attribute("router.selected_action", action)

        logger.info(
            "Classified request intent",
            extra={"event": "intent_classified", "intent": intent, "business_action": action},
        )
        return intent, action


# ---------------------------------------------------------------------------
# Stage 3: Business Action Executor
# ---------------------------------------------------------------------------
async def execute_business_action(
    action_type: str, question: str, simulate_mode: str
) -> BusinessActionResult:
    start = time.perf_counter()
    with observe_stage("business_action.execute", {"business.action_type": action_type}) as span:
        # Allow deterministic simulation of a database/dependency outage
        if simulate_mode == "db_outage" or "[simulate_db_outage]" in question.lower():
            await asyncio.sleep(0.12)
            logger.error(
                "Failed to execute business action: telemetry database connection refused",
                extra={
                    "event": "business_action_failed",
                    "action_type": action_type,
                    "db_host": "postgres-telemetry-prod.internal:5432",
                    "error_code": "ECONNREFUSED",
                },
            )
            raise UpstreamDatastoreError(
                message="Database connection refused while executing business action.",
                dependency="postgres-telemetry-prod",
            )

        if action_type == "create_incident_ticket":
            await asyncio.sleep(0.045)  # Simulate ticket creation API call
            severity = "P1-Critical" if "p1" in question.lower() or "outage" in question.lower() else "P2-High"
            ticket_id = f"INC-{uuid.uuid4().hex[:6].upper()}"
            ticket_record = {
                "ticket_id": ticket_id,
                "severity": severity,
                "title": question[:80],
                "assigned_team": "Platform-SRE-OnCall",
                "sla_response_target_minutes": 15 if severity == "P1-Critical" else 60,
                "service_metrics_snapshot": MOCK_SERVICE_METRICS["payment-gateway"],
            }
            INCIDENT_TICKETS_DB.append(ticket_record)
            span.set_attribute("ticket.id", ticket_id)
            span.set_attribute("ticket.severity", severity)
            summary = f"Created incident ticket {ticket_id} ({severity}) assigned to Platform-SRE-OnCall."
            payload = ticket_record

        elif action_type == "generate_sla_report":
            await asyncio.sleep(0.035)  # Simulate analytical query over service metrics
            breached = [
                svc for svc, data in MOCK_SERVICE_METRICS.items() if data["sla_status"] == "BREACHED"
            ]
            span.set_attribute("report.services_evaluated", len(MOCK_SERVICE_METRICS))
            span.set_attribute("report.breached_count", len(breached))
            summary = (
                f"Generated SLA report across {len(MOCK_SERVICE_METRICS)} services; "
                f"{len(breached)} service(s) currently breaching SLA ({', '.join(breached)})."
            )
            payload = {
                "report_id": f"REP-{uuid.uuid4().hex[:6].upper()}",
                "breached_services": breached,
                "metrics_snapshot": MOCK_SERVICE_METRICS,
            }

        else:
            await asyncio.sleep(0.020)
            summary = "Retrieved standard runbook context from platform knowledge base."
            payload = {
                "kb_article": "KB-402: Standard Observability & Incident Triage Runbook",
                "monitored_services": list(MOCK_SERVICE_METRICS.keys()),
            }

        exec_ms = round((time.perf_counter() - start) * 1000, 2)
        logger.info(
            "Executed business action successfully",
            extra={
                "event": "business_action_completed",
                "action_type": action_type,
                "execution_time_ms": exec_ms,
                "summary": summary,
            },
        )

        return BusinessActionResult(
            action_type=action_type,
            status="executed",
            summary=summary,
            payload=payload,
            execution_time_ms=exec_ms,
        )


# ---------------------------------------------------------------------------
# Stage 4: Resilient LLM Inference with Automated Retry & Fallback
# ---------------------------------------------------------------------------
async def call_llm_with_fallback(
    question: str, action_result: BusinessActionResult, simulate_mode: str
) -> Tuple[str, str, bool, TokenUsage]:
    """
    Demonstrates the mandatory engineering improvement:
    Attempts inference on the primary LLM within a dedicated child span.
    If the primary LLM times out or hits rate limits, records the span failure,
    increments Prometheus fallback metrics, logs a structured warning, and
    seamlessly falls back to a secondary low-latency model.
    """
    primary_model = "gpt-4o-primary"
    fallback_model = "claude-3-5-haiku-fallback"
    force_timeout = (
        simulate_mode == "llm_timeout" or "[simulate_timeout]" in question.lower()
    )

    with observe_stage("llm.generate_with_fallback") as parent_span:
        # Attempt 1: Primary LLM
        try:
            with tracer.start_as_current_span("llm.attempt.primary") as primary_span:
                primary_span.set_attribute("llm.model", primary_model)
                primary_span.set_attribute("llm.timeout_threshold_ms", 400)

                if force_timeout:
                    await asyncio.sleep(0.42)  # Simulate hung upstream socket exceeding 400ms SLA
                    timeout_exc = TimeoutError(
                        f"Primary model '{primary_model}' timed out after 420ms (SLA threshold: 400ms)"
                    )
                    primary_span.record_exception(timeout_exc)
                    primary_span.set_status(Status(StatusCode.ERROR, str(timeout_exc)))
                    raise timeout_exc

                await asyncio.sleep(0.11)  # Normal primary model latency (~110ms)
                primary_span.set_status(Status(StatusCode.OK))

            model_used = primary_model
            fallback_triggered = False

        except TimeoutError as exc:
            # Record telemetry for the failure & trigger automated fallback
            LLM_FALLBACK_TRIGGERS_TOTAL.labels(
                primary_model=primary_model,
                fallback_model=fallback_model,
                error_reason="upstream_timeout",
            ).inc()

            logger.warning(
                "Primary LLM timed out; executing automated fallback to secondary model",
                extra={
                    "event": "llm_fallback_triggered",
                    "primary_model": primary_model,
                    "fallback_model": fallback_model,
                    "error_reason": str(exc),
                },
            )

            # Attempt 2: Secondary Fallback LLM
            with tracer.start_as_current_span("llm.attempt.fallback") as fallback_span:
                fallback_span.set_attribute("llm.model", fallback_model)
                fallback_span.set_attribute("llm.fallback_reason", "primary_timeout")
                await asyncio.sleep(0.065)  # Fast fallback model latency (~65ms)
                fallback_span.set_status(Status(StatusCode.OK))

            model_used = fallback_model
            fallback_triggered = True

        # Synthesize grounded response incorporating the Business Action output
        answer = (
            f"[Action Executed: {action_result.summary}] "
            f"Based on live platform telemetry, payment-gateway is currently experiencing elevated "
            f"P95 latency (480ms) and a 2.4% error rate, while auth-service (42ms) and checkout-worker "
            f"(195ms) remain healthy. Relevant operational artifacts ({', '.join(action_result.payload.keys())}) "
            f"have been attached to this response."
        )

        prompt_tokens = max(25, len(question.split()) * 4 + 48)
        completion_tokens = max(35, len(answer.split()) * 2)
        usage = TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        )

        # Record Prometheus token counters and span attributes
        LLM_TOKENS_TOTAL.labels(model=model_used, token_type="prompt").inc(prompt_tokens)
        LLM_TOKENS_TOTAL.labels(model=model_used, token_type="completion").inc(completion_tokens)

        parent_span.set_attribute("llm.model_used", model_used)
        parent_span.set_attribute("llm.fallback_triggered", fallback_triggered)
        parent_span.set_attribute("llm.tokens.total", usage.total_tokens)

        logger.info(
            "LLM generation completed",
            extra={
                "event": "llm_generation_completed",
                "model_used": model_used,
                "fallback_triggered": fallback_triggered,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
        )

        return answer, model_used, fallback_triggered, usage


# ---------------------------------------------------------------------------
# Orchestrator Entrypoint called by POST /ask
# ---------------------------------------------------------------------------
async def process_ask_workflow(req: AskRequest) -> AskResponse:
    workflow_start = time.perf_counter()
    intent = "unclassified"
    action_type = "none"
    model_used = "none"

    try:
        cleaned_question = await validate_question_guardrails(req)
        intent, action_type = await classify_intent(cleaned_question)
        action_result = await execute_business_action(
            action_type=action_type,
            question=cleaned_question,
            simulate_mode=req.simulate_mode or "normal",
        )
        answer, model_used, fallback_triggered, token_usage = await call_llm_with_fallback(
            question=cleaned_question,
            action_result=action_result,
            simulate_mode=req.simulate_mode or "normal",
        )

        AI_WORKFLOW_REQUESTS_TOTAL.labels(
            intent=intent,
            business_action=action_type,
            model_used=model_used,
            status="success",
        ).inc()

        total_latency_ms = round((time.perf_counter() - workflow_start) * 1000, 2)
        trace_id, _ = get_current_trace_context()

        return AskResponse(
            answer=answer,
            intent=intent,
            model_used=model_used,
            fallback_triggered=fallback_triggered,
            business_action=action_result,
            token_usage=token_usage,
            latency_ms=total_latency_ms,
            request_id=get_request_id(),
            trace_id=trace_id,
        )

    except Exception:
        AI_WORKFLOW_REQUESTS_TOTAL.labels(
            intent=intent,
            business_action=action_type,
            model_used=model_used,
            status="error",
        ).inc()
        raise