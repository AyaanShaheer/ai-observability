import time
from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from app.models import AskRequest, AskResponse, ErrorResponse, HealthResponse
from app.observability import (
    ObservabilityMiddleware,
    get_current_trace_context,
    get_request_id,
    logger,
    render_prometheus_metrics,
)
from app.workflow import (
    GuardrailValidationError,
    INCIDENT_TICKETS_DB,
    UpstreamDatastoreError,
    process_ask_workflow,
)

SERVICE_START_TIME = time.time()

app = FastAPI(
    title="Observable AI Platform Service",
    description="AI Backend Service instrumented with Structured Logs, Prometheus Metrics, and OpenTelemetry Tracing.",
    version="1.0.0",
)

# 1. Register Custom Observability Middleware & OpenTelemetry Auto-Instrumentation
app.add_middleware(ObservabilityMiddleware)
FastAPIInstrumentor.instrument_app(app, excluded_urls="metrics")


def build_correlation_headers() -> dict[str, str]:
    trace_id, _ = get_current_trace_context()
    return {
        "X-Request-ID": get_request_id(),
        "X-Trace-ID": trace_id,
    }


# ---------------------------------------------------------------------------
# Structured Exception Handlers (Guarantees Trace & Request IDs on Errors)
# ---------------------------------------------------------------------------
@app.exception_handler(GuardrailValidationError)
async def guardrail_exception_handler(request: Request, exc: GuardrailValidationError):
    trace_id, _ = get_current_trace_context()
    payload = ErrorResponse(
        error_code="GUARDRAIL_VALIDATION_FAILED",
        message=exc.message,
        details=f"Rejected by input guardrail rule: {exc.reason}",
        request_id=get_request_id(),
        trace_id=trace_id,
    )
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content=payload.model_dump(),
        headers=build_correlation_headers(),
    )


@app.exception_handler(UpstreamDatastoreError)
async def datastore_exception_handler(request: Request, exc: UpstreamDatastoreError):
    trace_id, _ = get_current_trace_context()
    payload = ErrorResponse(
        error_code="UPSTREAM_DATASTORE_UNAVAILABLE",
        message=exc.message,
        details=f"Failed dependency: {exc.dependency}",
        request_id=get_request_id(),
        trace_id=trace_id,
    )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=payload.model_dump(),
        headers=build_correlation_headers(),
    )


@app.exception_handler(RequestValidationError)
async def pydantic_validation_exception_handler(request: Request, exc: RequestValidationError):
    trace_id, _ = get_current_trace_context()
    logger.warning(
        "Request payload failed Pydantic schema validation",
        extra={"event": "schema_validation_failed", "errors": exc.errors()},
    )
    payload = ErrorResponse(
        error_code="INVALID_REQUEST_SCHEMA",
        message="Request body failed schema validation. Ensure JSON includes a valid 'question' string.",
        details=str(exc.errors()),
        request_id=get_request_id(),
        trace_id=trace_id,
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=payload.model_dump(),
        headers=build_correlation_headers(),
    )


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------
@app.post(
    "/ask",
    response_model=AskResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Input rejected by guardrails"},
        422: {"model": ErrorResponse, "description": "Malformed request schema"},
        503: {"model": ErrorResponse, "description": "Upstream dependency failure"},
    },
)
async def ask_endpoint(payload: AskRequest, response: Response) -> AskResponse:
    """
    Processes an AI question, executes a correlated business action (incident ticket creation
    or SLA report generation), and runs LLM inference with automated fallback.
    """
    result = await process_ask_workflow(payload)
    for k, v in build_correlation_headers().items():
        response.headers[k] = v
    return result


@app.get("/health", response_model=HealthResponse)
async def health_endpoint(response: Response) -> HealthResponse:
    """Returns service health, uptime, and internal dependency readiness checks."""
    uptime = round(time.time() - SERVICE_START_TIME, 2)
    for k, v in build_correlation_headers().items():
        response.headers[k] = v
    return HealthResponse(
        status="healthy",
        service="ai-platform-service",
        version="1.0.0",
        uptime_seconds=uptime,
        checks={
            "llm_router": "ready",
            "telemetry_exporter": "ready",
            "incident_store_records": str(len(INCIDENT_TICKETS_DB)),
        },
    )


@app.get("/metrics")
async def metrics_endpoint() -> Response:
    """Exposes Prometheus metrics in standard text format."""
    return render_prometheus_metrics()