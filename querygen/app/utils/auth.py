from fastapi.security.api_key import APIKeyHeader
from fastapi import Security, HTTPException, Depends, Request
from fastapi.concurrency import run_in_threadpool
from starlette.status import HTTP_403_FORBIDDEN, HTTP_401_UNAUTHORIZED
from pydantic import BaseModel
import hmac
import os
from dotenv import load_dotenv
import logging
from app.utils.license_cache import get_license_cache_service
from app.utils.supabase_client import supabase
from app.utils import installs
from app.db import SessionType

load_dotenv()

logger = logging.getLogger(__name__)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
internal_api_key_header = APIKeyHeader(name="X-Internal-Api-Key", auto_error=False)


async def verify_internal_key(
    internal_api_key_header: str = Security(internal_api_key_header),
):
    """
    Restrict an endpoint to trusted internal callers (e.g. the website backend)
    via a shared secret header. Required for endpoints that use the Supabase
    service-role client or mutate CRM lead state, since those have no per-user
    auth of their own and would otherwise be abusable by anyone who can reach
    the API (OTP/email spam, unauthorized metadata writes, billing-state flips).
    """
    expected_key = os.getenv("INTERNAL_API_KEY")
    if not expected_key:
        logger.error("INTERNAL_API_KEY is not configured")
        raise HTTPException(
            status_code=500, detail="Internal API key not configured"
        )

    if not internal_api_key_header or not hmac.compare_digest(
        internal_api_key_header, expected_key
    ):
        raise HTTPException(
            status_code=HTTP_403_FORBIDDEN, detail="Invalid or missing internal API key"
        )


async def require_supabase_user(request: Request):
    """
    Authenticate a browser caller via their Supabase access token (JWT).

    The website's per-user billing endpoints are reachable directly from the
    browser, so they can't carry the internal service key. Instead the client
    sends its Supabase session token as ``Authorization: Bearer <jwt>`` and we
    resolve it to the owning user. Endpoints then act only on the *verified*
    identity (never on an attacker-supplied id/email), which closes the
    email/UUID enumeration hole on those endpoints.

    Returns the authenticated Supabase user (has ``id`` and ``email``).
    """
    if supabase is None:
        logger.error("Supabase client not configured; cannot verify user session")
        raise HTTPException(status_code=500, detail="Auth backend not configured")

    auth_header = request.headers.get("Authorization") or ""
    scheme, _, token = auth_header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed bearer token",
        )

    try:
        # supabase-py's get_user() makes a blocking HTTP call to GoTrue; run it
        # off the event loop so a slow validation can't stall other requests.
        user_response = await run_in_threadpool(supabase.auth.get_user, token)
    except Exception:
        logger.warning("Supabase session validation failed", exc_info=True)
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED, detail="Invalid or expired session"
        )

    user = getattr(user_response, "user", None)
    if user is None or not getattr(user, "id", None):
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED, detail="Invalid or expired session"
        )
    return user


def _admit_install(request: Request, session, principal, identity):
    """Record the calling database and decide whether it may continue. The
    outcome lands on request.state for the billing layer and /install/hello."""
    install = installs.touch_install(session, identity, principal)
    access, grace_until = installs.install_access(install, principal)
    if install is not None and session.is_modified(install):
        session.add(install)
        session.commit()
    request.state.principal = principal
    request.state.install = install
    request.state.license_access = (access, grace_until)
    if access == "restricted" and installs.path_gated(request.url.path):
        raise HTTPException(
            status_code=402,
            detail=installs.RESTRICTED_MESSAGE,
            headers={"X-License-Status": "restricted"},
        )


async def get_api_key(
    request: Request,
    session: SessionType,
    api_key_header: str = Security(api_key_header),
):
    """
    Validate API key using cached license validation.

    This function uses the license cache service to avoid rate limiting.
    It will:
    1. Check cache first (6-hour TTL)
    2. Only call Polar if cache expired
    3. Fall back to cache if Polar fails
    """
    if not api_key_header:
        raise HTTPException(
            status_code=HTTP_403_FORBIDDEN, detail="X-API-Key header is required"
        )

    identity = installs.identity_from_headers(request.headers)
    principal = installs.resolve_principal(session, api_key_header)
    if principal is not None:
        # A shipped key or instance token: no Polar involved.
        install_compile = principal.kind == "install" and request.url.path in installs.ONTO_COMPILE_PATHS
        if not (install_compile or installs.path_allowed(request.url.path)):
            raise HTTPException(
                status_code=HTTP_403_FORBIDDEN, detail="This key cannot call that."
            )
        _admit_install(request, session, principal, identity)
        return api_key_header
    if api_key_header.startswith((installs.EMBEDDED_PREFIX, installs.TOKEN_PREFIX)):
        # Ours by shape but unknown: no point asking Polar.
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="Could not validate API KEY")

    try:
        cache_service = get_license_cache_service()
        result = await cache_service.validate_license_key(
            session=session, license_key=api_key_header
        )

        if result.get("validated", False):
            # Log cache performance for monitoring
            if result.get("from_cache"):
                logger.debug(
                    f"Cache hit for API key validation: {api_key_header[:8]}..."
                )
            else:
                logger.info(f"Validated API key with Polar: {api_key_header[:8]}...")

            if identity is not None:
                try:
                    _admit_install(
                        request, session, installs.Principal("license"), identity
                    )
                except HTTPException:
                    raise
                except Exception:
                    logger.exception("could not record install for a license key")
            return api_key_header
        else:
            logger.warning(
                f"API key validation failed: {api_key_header[:8]}... - "
                f"Error: {result.get('error', 'Unknown')}"
            )
            raise HTTPException(
                status_code=HTTP_403_FORBIDDEN, detail="Could not validate API KEY"
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Unexpected error during API key validation: {str(e)}")
        raise HTTPException(
            status_code=HTTP_403_FORBIDDEN, detail="Could not validate API KEY"
        )
