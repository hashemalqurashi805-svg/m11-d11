import uuid
import json
import logging
import time
import contextvars
from fastapi import FastAPI, Request
from pydantic import BaseModel
from prometheus_client import Counter, Histogram, Gauge, make_asgi_app
from starlette.middleware.base import BaseHTTPMiddleware

# 1. Module-level declarations
request_id_var = contextvars.ContextVar("request_id", default="")
logger = logging.getLogger("app")

# Declare Metrics
requests_total = Counter(
    "requests_total", "Total HTTP requests", ["path", "status"]
)
request_latency_seconds = Histogram(
    "request_latency_seconds", "Request latency in seconds", ["path"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10]
)
inflight_requests = Gauge("inflight_requests", "In-flight requests")

# 2. Middlewares

class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        req_id = uuid.uuid4().hex
        token = request_id_var.set(req_id)
        response = await call_next(request)
        response.headers["X-Request-ID"] = req_id
        request_id_var.reset(token)
        return response

class StructuredLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start_time = time.perf_counter()
        response = await call_next(request)
        latency = (time.perf_counter() - start_time) * 1000
        
        log_data = {
            "ts": time.time(),
            "level": "INFO",
            "request_id": request_id_var.get(),
            "path": request.url.path,
            "status": response.status_code,
            "latency_ms": round(latency, 2)
        }
        logger.info(json.dumps(log_data))
        return response

class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        inflight_requests.inc()
        start_time = time.perf_counter()
        try:
            response = await call_next(request)
            return response
        finally:
            elapsed = time.perf_counter() - start_time
            requests_total.labels(path=request.url.path, status=str(response.status_code)).inc()
            request_latency_seconds.labels(path=request.url.path).observe(elapsed)
            inflight_requests.dec()

# 3. FastAPI App Setup
app = FastAPI(title="M11 Drill — Toy FastAPI Service")

# Add Middlewares (Last added is the outermost)
app.add_middleware(MetricsMiddleware)
app.add_middleware(StructuredLoggingMiddleware)
app.add_middleware(RequestIdMiddleware)

# Mount metrics endpoint
app.mount("/metrics", make_asgi_app())

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

class EchoRequest(BaseModel):
    message: str

@app.post("/echo")
def echo(req: EchoRequest):
    return {"echo": req.message}

@app.get("/sum")
def sum_endpoint(a: int = 0, b: int = 0):
    return {"sum": a + b}