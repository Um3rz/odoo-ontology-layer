"""Async httpx client for a tenant's registered Odoo (plan 0.6, contract 3.9).

Plain HTTPS Bearer with an Odoo-minted token. Never sends or sees a mobile API
key. Redirects are not followed (the token must not travel to another host).
Nothing here logs a token, a body or a row.
"""
import asyncio
import logging

import httpx

from app.odoo.tokens import has_token_shape

log = logging.getLogger(__name__)

RETRY_STATUS = {502, 503, 504}


class OdooError(Exception):
    """Odoo refused or was unreachable. `status` is the HTTP status, 0 if none. `code` is Odoo's
    {"error": code} value when it sent one (plan 3.5 maps these codes), else ""."""

    def __init__(self, status: int, message: str = "", code: str = ""):
        super().__init__(message or f"odoo status {status}")
        self.status = status
        self.code = code


def _error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    code = body.get("error") if isinstance(body, dict) else None
    return code if isinstance(code, str) and len(code) <= 64 else ""


class OdooClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 15.0,
        retries: int = 2,
        backoff: float = 0.3,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not has_token_shape(token):
            raise ValueError("not an Onto token")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.retries = retries
        self.backoff = backoff
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=5.0),
            follow_redirects=False,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "OdooClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def request(self, method: str, path: str, json: dict | list | None = None, *, want: type = dict):
        """Call `/onto/v1/...`. Retries only connection errors, timeouts and 502-504.

        The body must be a JSON `want`: an object by default, a list for store/read (plan 3.6).
        """
        if not path.startswith("/onto/v1/"):
            raise ValueError("only /onto/v1/ routes may be called")
        last: OdooError | None = None
        for attempt in range(self.retries + 1):
            if attempt:
                await asyncio.sleep(self.backoff * 2 ** (attempt - 1))
            try:
                r = await self._http.request(
                    method, path, json=json,
                    headers={"Authorization": f"Bearer {self._token}"},
                )
            except httpx.TransportError as e:  # includes timeouts
                last = OdooError(0, type(e).__name__)
                continue
            if r.status_code in RETRY_STATUS:
                last = OdooError(r.status_code)
                continue
            if r.status_code >= 400:
                raise OdooError(r.status_code, code=_error_code(r))
            try:
                data = r.json()
            except ValueError:
                raise OdooError(r.status_code, "non-JSON response")
            if not isinstance(data, want):
                raise OdooError(r.status_code, "unexpected response shape")
            return data
        log.warning("odoo call failed after retries path=%s status=%s", path, last.status if last else None)
        raise last or OdooError(0)

    async def whoami(self) -> dict:
        return await self.request("GET", "/onto/v1/whoami")
