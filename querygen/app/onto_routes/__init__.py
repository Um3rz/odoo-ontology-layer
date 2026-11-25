"""Onto router mounting (plan 0.6). Later phases add their routers here."""
from fastapi import APIRouter, Depends, FastAPI

from app.onto_routes.compile import router as compile_router
from app.onto_routes.drift import router as drift_router
from app.onto_routes.parity import router as parity_router
from app.onto_routes.ontology import router as ontology_router
from app.onto_routes.systems import router as systems_router
from app.utils.browser_auth import (
    OntoAuth,
    OntoAuthError,
    OntoCORSMiddleware,
    browser_auth,
    onto_auth_error_handler,
)

router = APIRouter(prefix="/onto", tags=["onto"])
router.include_router(compile_router)  # POST /onto/compile (plan 1.6)
router.include_router(parity_router)  # POST /onto/jobs/parity (plan 1.8)
router.include_router(drift_router)  # POST /onto/jobs/drift (plan 2.5)


@router.get("/health")
async def health():
    """No auth, content-free liveness check the browser can call (CORS covers /onto)."""
    return {"ok": True}


@router.get("/session")
async def session(auth: OntoAuth = Depends(browser_auth)):
    """Proves the browser's token end to end (Odoo whoami). Content-free."""
    return {"tenant_id": str(auth.tenant_id), "ai_enabled": auth.ai_enabled}


def mount(app: FastAPI) -> None:
    app.add_exception_handler(OntoAuthError, onto_auth_error_handler)
    app.include_router(router)
    app.include_router(ontology_router)  # /ontology/* (plan 1.9): its paths are not under /onto
    app.include_router(systems_router)  # /systems/* (plan 3.3): Odoo's crons, bot token
    app.add_middleware(OntoCORSMiddleware)
