"""
main.py — FastAPI application entrypoint.
Structured logging: every request logs trace_id, store_id, endpoint, latency_ms, status_code.
"""
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import os

from app.database import init_db
from app.ingestion import router as ingestion_router
from app.metrics   import router as metrics_router
from app.funnel    import router as funnel_router
from app.anomalies import router as anomalies_router
from app.health    import router as health_router

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level  = logging.INFO,
    format = "%(message)s",
)
logger = logging.getLogger("store_intelligence")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info(json.dumps({"event": "startup", "message": "Store Intelligence API ready"}))
    yield
    logger.info(json.dumps({"event": "shutdown"}))


app = FastAPI(
    title       = "Store Intelligence API",
    version     = "1.0.0",
    description = "Apex Retail offline store analytics",
    lifespan    = lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins  = ["*"],
    allow_methods  = ["*"],
    allow_headers  = ["*"],
)


# ── Structured request logging middleware ─────────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    trace_id  = str(uuid.uuid4())[:8]
    store_id  = request.path_params.get("store_id")
    start     = time.perf_counter()

    try:
        response = await call_next(request)
    except Exception as exc:
        logger.error(json.dumps({
            "trace_id":   trace_id,
            "store_id":   store_id,
            "endpoint":   request.url.path,
            "latency_ms": int((time.perf_counter() - start) * 1000),
            "status_code": 500,
            "error":      str(exc),
        }))
        return JSONResponse(
            status_code = 503,
            content     = {"error": "service_unavailable", "detail": "Internal error. Check logs."},
        )

    latency = int((time.perf_counter() - start) * 1000)
    event_count = None
    if request.url.path == "/events/ingest" and request.method == "POST":
        event_count = "?"  # filled by ingestion handler

    logger.info(json.dumps({
        "trace_id":    trace_id,
        "store_id":    store_id,
        "endpoint":    request.url.path,
        "method":      request.method,
        "latency_ms":  latency,
        "status_code": response.status_code,
        **({"event_count": event_count} if event_count else {}),
    }))

    return response


# ── Graceful DB error handler ─────────────────────────────────────────────────
@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception):
    import sqlite3
    if isinstance(exc, (sqlite3.OperationalError, sqlite3.DatabaseError)):
        return JSONResponse(
            status_code = 503,
            content     = {
                "error":   "database_unavailable",
                "detail":  "Database is temporarily unavailable.",
                "path":    str(request.url.path),
            },
        )
    return JSONResponse(
        status_code = 500,
        content     = {"error": "internal_server_error", "detail": "An unexpected error occurred."},
    )


# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(ingestion_router)
app.include_router(metrics_router)
app.include_router(funnel_router)
app.include_router(anomalies_router)
app.include_router(health_router)


# ── Dashboard static files ────────────────────────────────────────────────────
dashboard_dir = os.path.join(os.path.dirname(__file__), "..", "dashboard")
if os.path.isdir(dashboard_dir):
    app.mount("/static", StaticFiles(directory=dashboard_dir), name="static")

    @app.get("/")
    def dashboard():
        return FileResponse(os.path.join(dashboard_dir, "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
