"""Querygen's ontology layer on its own: native BI chat steps, the Onto routes and the agent runtime.

Extracted from the full querygen-api service, which also serves datasets, dashboards and billing.
"""
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

load_dotenv()

from app.agents.service import router as agents_router, start_loop, stop_loop  # noqa: E402
from app.db import create_db_and_tables  # noqa: E402
from app.onto_routes import mount as mount_onto  # noqa: E402
from app.ontology.loader import load_release  # noqa: E402
from app.routes import native_bi  # noqa: E402
from app.utils.browser_auth import NonOntoCORSMiddleware  # noqa: E402

ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "")
if ALLOWED_ORIGIN == "*":
    origins, allow_credentials = ["*"], False  # a wildcard origin must never carry credentials
else:
    origins = [origin.strip() for origin in ALLOWED_ORIGIN.split(",") if origin.strip()]
    allow_credentials = True
IS_PROD = os.getenv("ENVIRONMENT") != "DEV"


@asynccontextmanager
async def lifespan(_: FastAPI):
    load_release()  # an invalid ontology template fails startup here
    create_db_and_tables()
    agent_loop = start_loop()  # requeue, claim, outbox flush (needs REDIS_URL)
    yield
    await stop_loop(agent_loop)


app = FastAPI(
    lifespan=lifespan,
    docs_url=None if IS_PROD else "/docs",
    redoc_url=None,
    openapi_url=None if IS_PROD else "/openapi.json",
)
app.add_middleware(
    NonOntoCORSMiddleware,
    allow_origins=origins,
    allow_credentials=allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    headers = response.headers
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "no-referrer")
    headers.setdefault("Cache-Control", "no-store")
    if IS_PROD:
        headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.get("/health")
async def health():
    return {"ok": True}


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    logging.getLogger("app").exception("Unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal error"})


app.include_router(native_bi.router)
mount_onto(app)
app.include_router(agents_router)
