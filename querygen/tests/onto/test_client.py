import httpx
import pytest

from app.odoo.client import OdooClient, OdooError

TOKEN = "aaa.bbb"
BASE = "https://odoo.example.test"


def client(handler, **kw):
    kw.setdefault("backoff", 0)
    return OdooClient(BASE, TOKEN, transport=httpx.MockTransport(handler), **kw)


async def test_whoami_sends_bearer_to_the_registered_base_only():
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        return httpx.Response(200, json={"ai_enabled": True})

    async with client(handler) as c:
        assert await c.whoami() == {"ai_enabled": True}
    (req,) = seen
    assert str(req.url) == f"{BASE}/onto/v1/whoami" and req.method == "GET"
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    assert "x-od-key" not in req.headers  # never a mobile key


async def test_retries_are_bounded_and_then_succeed():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, json={"ok": 1})

    async with client(handler, retries=2) as c:
        assert await c.whoami() == {"ok": 1}
    assert len(calls) == 3


async def test_gives_up_after_the_retry_budget():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503)

    async with client(handler, retries=2) as c:
        with pytest.raises(OdooError) as e:
            await c.whoami()
    assert e.value.status == 503 and len(calls) == 3


async def test_transport_errors_and_timeouts_are_retried():
    calls = []

    def handler(req):
        calls.append(1)
        raise httpx.ConnectTimeout("slow")

    async with client(handler, retries=1) as c:
        with pytest.raises(OdooError) as e:
            await c.whoami()
    assert e.value.status == 0 and len(calls) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500])
async def test_client_errors_are_not_retried(status):
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(status)

    async with client(handler, retries=3) as c:
        with pytest.raises(OdooError) as e:
            await c.whoami()
    assert e.value.status == status and len(calls) == 1


async def test_redirects_are_not_followed():
    def handler(req):
        if req.url.host == "odoo.example.test":
            return httpx.Response(302, headers={"location": "https://evil.test/steal"})
        raise AssertionError("token followed a redirect")

    async with client(handler) as c:
        with pytest.raises(OdooError) as e:
            await c.whoami()
    assert e.value.status == 302


async def test_non_json_and_non_object_bodies_are_errors():
    for resp in (httpx.Response(200, text="<html>"), httpx.Response(200, json=[1])):
        async with client(lambda r, resp=resp: resp) as c:
            with pytest.raises(OdooError):
                await c.whoami()


async def test_only_onto_routes_may_be_called():
    async with client(lambda r: httpx.Response(200, json={})) as c:
        for path in ("/mobile/v1/ping", "/web/login", "//evil.test/onto/v1/x"):
            with pytest.raises(ValueError):
                await c.request("GET", path)


def test_a_non_token_is_refused_at_construction():
    with pytest.raises(ValueError):
        OdooClient(BASE, "an-api-key-without-a-dot")


async def test_no_token_in_logs(caplog):
    with caplog.at_level("DEBUG"):
        async with client(lambda r: httpx.Response(503), retries=1) as c:
            with pytest.raises(OdooError):
                await c.whoami()
    assert TOKEN not in caplog.text
