import logging
import os
from typing import Optional

import httpx
from dotenv import load_dotenv
from supabase import Client, create_client

logger = logging.getLogger(__name__)

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# GoTrue sends magic-link / OTP emails synchronously through the configured SMTP
# provider before responding, so the auth endpoints can be far slower than the
# httpx default (~5s read). A slow-but-successful send would otherwise surface as
# a ReadTimeout and 500 the caller. Give the auth transport a generous timeout.
_AUTH_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _extend_auth_timeout(client: Client) -> None:
    """Replace the GoTrue http client with one that has a longer read timeout.

    Guarded: if the internal attribute ever moves/renames across supabase-py
    versions we log and carry on with the default timeout rather than crash at
    import time.
    """
    try:
        client.auth._http_client = httpx.Client(
            verify=True,
            follow_redirects=True,
            http2=True,
            timeout=_AUTH_HTTP_TIMEOUT,
        )
    except Exception:  # pragma: no cover - defensive
        logger.warning(
            "Could not extend Supabase auth client timeout; using library default.",
            exc_info=True,
        )


supabase: Optional[Client]
if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    _extend_auth_timeout(supabase)
else:
    supabase = None
    logger.warning(
        "SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY is not set; supabase client disabled."
    )
