"""POST /onto/jobs/parity (plan 1.8, contract 3.9 jobs). The tenant registry and job_run live in the scratch
database (conftest.py). Odoo is a stub (httpx.MockTransport) that answers whoami and the parity run."""
import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app import db
from app.db.onto import JobRun, Tenant
from app.odoo.client import OdooClient
from app.onto_routes import mount, parity
from app.utils import browser_auth as ba

ODOO = "https://odoo.example.test"
DB_UUID = "33a1b60e-0000-0000-0000-000000000018"
BOT = "bot.sig"  # shape only: querygen never verifies the token, Odoo's whoami does
BOT_GROUP = "odoo_dashboards_agents.group_onto_bot"
HEADERS = {"Authorization": f"Bearer {BOT}", "X-Odoo-Url": ODOO}
WHO = {"database": {"uuid": DB_UUID}, "groups": [BOT_GROUP], "ai_enabled": True, "user": {"id": 9}}
REPLY = {
    "results": [
        {"object": "stock_position", "signature": "a" * 64, "match": True,
         "values": {"orm": 3, "live": 3, "stored": []}, "tolerance": 0},
        {"object": "stock_position", "signature": "b" * 64, "match": False,
         "values": {"orm": 2, "live": 9, "stored": []}, "tolerance": 0},
    ],
    "suspended": 1,
    "lifted": 0,
}
COUNTS = {"objects": 1, "checks": 2, "matched": 1, "mismatched": 1, "suspended": 1, "lifted": 0}


class StubOdoo:
    """The tenant's Odoo as the job sees it. Counts and records every call; never sees a body it did not send."""

    def __init__(self):
        self.who, self.who_status = copy.deepcopy(WHO), 200
        self.run_status, self.run_body = 200, REPLY
        self.calls = []

    def handler(self, req: httpx.Request):
        self.calls.append((req.method, req.url.path, req.headers.get("authorization")))
        if req.url.path == "/onto/v1/whoami":
            return httpx.Response(self.who_status, json=self.who)
        if req.url.path == "/onto/v1/parity/run":
            return httpx.Response(self.run_status, json=self.run_body)
        return httpx.Response(404)

    def paths(self):
        return [path for _, path, _ in self.calls]


@pytest.fixture()
def env(engine, session, clean_tenants, monkeypatch):
    stub = StubOdoo()
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(parity, "odoo_client_factory", lambda base, tok: OdooClient(
        base, tok, transport=httpx.MockTransport(stub.handler), backoff=0, retries=0))
    monkeypatch.setattr(ba, "token_limiter", ba.RateLimiter(100, 60))
    monkeypatch.setattr(ba, "tenant_limiter", ba.RateLimiter(100, 60))
    tenant = Tenant(odoo_base_url=ODOO, db_uuid=DB_UUID)
    session.add(tenant)
    session.commit()
    app = FastAPI()
    app.dependency_overrides[db.get_session] = lambda: session
    mount(app)
    return SimpleNamespace(client=TestClient(app), stub=stub, session=session, tenant_id=tenant.id)


def _post(env, headers=HEADERS):
    return env.client.post("/onto/jobs/parity", headers=headers)


def _rows(env):
    return env.session.exec(select(JobRun)).all()


def test_the_job_asks_odoo_to_run_the_check_and_keeps_counts_only(env):
    resp = _post(env)
    assert resp.status_code == 200, resp.text
    assert (resp.json()["state"], resp.json()["result"]) == ("done", COUNTS)
    assert env.stub.paths() == ["/onto/v1/whoami", "/onto/v1/parity/run"]
    assert all(auth == f"Bearer {BOT}" for _, _, auth in env.stub.calls)
    (row,) = _rows(env)
    assert (row.tenant_id, row.job, row.state, row.result) == (env.tenant_id, "parity", "done", COUNTS)
    # Nothing an object, a signature or a value was sent for is kept or returned.
    kept = json.dumps([resp.text, row.result])
    assert "stock_position" not in kept and "signature" not in kept and '"values"' not in kept


def test_a_live_run_blocks_another_and_odoo_is_not_asked(env):
    env.session.add(JobRun(tenant_id=env.tenant_id, job="parity", state="running",
                           heartbeat_at=datetime.now(timezone.utc)))
    env.session.commit()
    resp = _post(env)
    assert (resp.status_code, resp.json()) == (409, {"error": "already_running"})
    assert env.stub.paths() == ["/onto/v1/whoami"]  # only the token check ran


def test_a_stale_running_row_does_not_block_a_new_run(env):
    env.session.add(JobRun(tenant_id=env.tenant_id, job="parity", state="running",
                           heartbeat_at=datetime.now(timezone.utc) - timedelta(hours=1)))
    env.session.commit()
    assert _post(env).status_code == 200


def test_an_odoo_failure_is_recorded_as_failed_and_not_retried(env):
    env.stub.run_status = 503
    resp = _post(env)
    assert (resp.status_code, resp.json()) == (502, {"error": "odoo_unavailable"})
    assert env.stub.paths().count("/onto/v1/parity/run") == 1  # a parity run writes, so it is never retried
    (row,) = _rows(env)
    assert (row.state, row.result["error"]) == ("failed", "odoo_unavailable")


def test_a_reply_of_the_wrong_shape_is_a_failed_run(env):
    env.stub.run_body = {"results": "not a list"}
    resp = _post(env)
    assert (resp.status_code, resp.json()) == (502, {"error": "odoo_unexpected"})
    (row,) = _rows(env)
    assert (row.state, row.result) == ("failed", {"error": "odoo_unexpected"})


def test_only_the_onto_bot_of_the_registered_database_may_run_the_job(env):
    no_bearer = _post(env, {"X-Odoo-Url": ODOO})
    api_key = _post(env, {"Authorization": "Bearer " + "ab" * 16, "X-Odoo-Url": ODOO})
    unknown = _post(env, {"Authorization": f"Bearer {BOT}", "X-Odoo-Url": "https://other.example.test"})
    assert [no_bearer.status_code, api_key.status_code, unknown.status_code] == [401, 401, 403]
    assert (no_bearer.json(), unknown.json()) == ({"error": "invalid_token"}, {"error": "unknown_tenant"})
    assert env.stub.paths() == []  # refused before Odoo is asked anything

    refusals = {
        "not_the_bot": {"groups": ["base.group_user"]},  # a person's token, not the bot's
        "tenant_mismatch": {"database": {"uuid": "some-other-database"}},
        "ai_features_off": {"ai_enabled": False},
    }
    for error, change in refusals.items():
        env.stub.who = dict(copy.deepcopy(WHO), **change)
        resp = _post(env)
        assert (resp.status_code, resp.json()) == (403, {"error": error}), error
        assert "/onto/v1/parity/run" not in env.stub.paths()

    env.stub.who, env.stub.who_status = copy.deepcopy(WHO), 401  # Odoo itself refuses the token
    resp = _post(env)
    assert (resp.status_code, resp.json()) == (401, {"error": "invalid_token"})
    assert _rows(env) == []  # nothing was started
