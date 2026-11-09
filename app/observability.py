import contextvars
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

# ---------------------------------------------------------------------------
# 1. Context Variables for Request Correlation
# ---------------------------------------------------------------------------
request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="system")


def get_request_id() -> str:
    return request_id_ctx.get()


def get_current_trace_context() -> tuple[str, str]:
    """Extracts active OpenTelemetry trace_id and span_id as hex strings."""
    span = trace.get_current_span()
    if not span:
        return "0" * 32, "0" * 16
    span_context = span.get_span_context()
    if not span_context or not span_context.is_valid:
        return "0" * 32, "0" * 16
    return format(span_context.trace_id, "032x"), format(span_context.span_id, "016x")


# ---------------------------------------------------------------------------
# 2. Structured JSON Logging with Automatic Trace & Request ID Injection
# ---------------------------------------------------------------------------
class StructuredJSONFormatter(logging.Formatter):
    """Outputs single-line JSON logs enriched with request_id, trace_id, and span_id."""

    RESERVED_ATTRS = {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        trace_id, span_id = get_current_trace_context()
        log_payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": "ai-platform-service",
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": get_request_id(),
            "trace_id": trace_id,
            "span_id": span_id,
            "module": f"{record.module}:{record.lineno}",
        }

        # Attach any extra structured fields passed via logger.info("...", extra={...})
        for key, value in record.__dict__.items():
            if key not in self.RESERVED_ATTRS and not key.startswith("_"):
                log_payload[key] = value

        if record.exc_info:
            log_payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_payload, default=str)


def setup_logging(log_level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("ai_platform")
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    logger.handlers.clear()
    logger.propagate = False

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredJSONFormatter())
    logger.addHandler(handler)
    return logger


logger = setup_logging(os.getenv("LOG_LEVEL", "INFO"))


# ---------------------------------------------------------------------------
# 3. OpenTelemetry Distributed Tracing Setup
# ---------------------------------------------------------------------------
def setup_tracing(service_name: str = "ai-platform-service") -> trace.Tracer:
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": "1.0.0",
            "deployment.environment": os.getenv("ENVIRONMENT", "local-demo"),
        }
    )
    provider = TracerProvider(resource=resource)

    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318/v1/traces")
    enable_otlp = os.getenv("ENABLE_OTLP_EXPORT", "true").lower() == "true"

    if enable_otlp:
        otlp_exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
        provider.add_span_processor(
            BatchSpanProcessor(
                otlp_exporter,
                max_queue_size=2048,
                schedule_delay_millis=1000,
            )
        )

    if os.getenv("ENABLE_CONSOLE_TRACES", "false").lower() == "true":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)
    return trace.get_tracer(service_name)


tracer = setup_tracing()


# ---------------------------------------------------------------------------
# 4. Prometheus Metrics Definitions (Low-Cardinality Bounded Labels)
# ---------------------------------------------------------------------------
HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests processed by endpoint, method, and status code",
    ["method", "endpoint", "http_status"],
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "End-to-end HTTP request latency in seconds",
    ["method", "endpoint"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.5, 5.0),
)

HTTP_IN_FLIGHT_REQUESTS = Gauge(
    "http_in_flight_requests",
    "Number of HTTP requests currently being processed",
)

AI_WORKFLOW_REQUESTS_TOTAL = Counter(
    "ai_workflow_requests_total",
    "Total AI workflow executions categorized by intent, action, model, and status",
    ["intent", "business_action", "model_used", "status"],
)

AI_STAGE_LATENCY_SECONDS = Histogram(
    "ai_stage_latency_seconds",
    "Latency distribution of individual stages inside the AI pipeline",
    ["stage"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 0.8, 1.2, 2.0, 3.5),
)

LLM_TOKENS_TOTAL = Counter(
    "llm_tokens_total",
    "Total LLM tokens consumed by model and token type (prompt vs completion)",
    ["model", "token_type"],
)

LLM_FALLBACK_TRIGGERS_TOTAL = Counter(
    "llm_fallback_triggers_total",
    "Count of primary LLM failures that triggered retry or fallback logic",
    ["primary_model", "fallback_model", "error_reason"],
)


def render_prometheus_metrics() -> Response:
    """Generates the Prometheus text format response for GET /metrics."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# 5. FastAPI Observability & Correlation Middleware
# ---------------------------------------------------------------------------
class ObservabilityMiddleware(BaseHTTPMiddleware):
    """
    Intercepts every HTTP request to:
    1. Extract or generate X-Request-ID and bind it to contextvars.
    2. Attach correlation attributes to the active OpenTelemetry span.
    3. Measure end-to-end latency and emit Prometheus RED metrics.
    4. Emit structured request start/completion JSON logs.
    5. Inject X-Request-ID and X-Trace-ID headers into the HTTP response.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        req_id = request.headers.get("X-Request-ID") or f"req-{uuid.uuid4().hex[:12]}"
        token = request_id_ctx.set(req_id)

        start_time = time.perf_counter()
        path = request.url.path
        method = request.method

        current_span = trace.get_current_span()
        if current_span and current_span.is_recording():
            current_span.set_attribute("http.request_id", req_id)
            current_span.set_attribute("http.route", path)

        trace_id, _ = get_current_trace_context()

        # Skip noisy access logs for /metrics scrapes if desired, or log at DEBUG
        if path != "/metrics":
            logger.info(
                "Incoming HTTP request",
                extra={
                    "event": "request_started",
                    "http_method": method,
                    "http_path": path,
                    "client_ip": request.client.host if request.client else "unknown",
                },
            )

        HTTP_IN_FLIGHT_REQUESTS.inc()
        status_code = 500

        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception as exc:
            status_code = 500
            if current_span and current_span.is_recording():
                current_span.record_exception(exc)
            logger.error(
                "Unhandled exception during request processing",
                exc_info=True,
                extra={"event": "request_unhandled_error", "http_path": path},
            )
            raise
        finally:
            duration_s = time.perf_counter() - start_time
            duration_ms = round(duration_s * 1000, 2)
            HTTP_IN_FLIGHT_REQUESTS.dec()

            # Record Prometheus RED metrics
            HTTP_REQUESTS_TOTAL.labels(
                method=method,
                endpoint=path,
                http_status=str(status_code),
            ).inc()
            HTTP_REQUEST_DURATION_SECONDS.labels(
                method=method,
                endpoint=path,
            ).observe(duration_s)

            if path != "/metrics":
                log_fn = logger.warning if status_code >= 400 else logger.info
                log_fn(
                    "Completed HTTP request",
                    extra={
                        "event": "request_completed",
                        "http_method": method,
                        "http_path": path,
                        "http_status": status_code,
                        "latency_ms": duration_ms,
                    },
                )

            request_id_ctx.reset(token)