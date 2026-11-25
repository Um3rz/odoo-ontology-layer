"""Shared fixtures: a throwaway Postgres database created on the server named by
DATABASE_URL, migrated with `alembic upgrade head`, and dropped afterwards."""
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlmodel import Session

ROOT = Path(__file__).resolve().parents[2]


def _admin_url():
    from app import db

    url = make_url(db.engine.url.render_as_string(hide_password=False))
    # "localhost" can stall ~20 s per connection on hosts where ::1 is unreachable.
    return url.set(host="127.0.0.1") if url.host == "localhost" else url


@pytest.fixture(scope="session")
def scratch_url():
    admin = _admin_url()
    name = f"onto_test_{uuid.uuid4().hex[:10]}"
    root = create_engine(admin, isolation_level="AUTOCOMMIT")
    with root.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = admin.set(database=name)
    url_str = url.render_as_string(hide_password=False)
    r = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT, env={**os.environ, "DATABASE_URL": url_str},
        capture_output=True, text=True,
    )
    try:
        assert r.returncode == 0, r.stderr[-2000:]
        yield url_str
    finally:
        with root.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        root.dispose()


@pytest.fixture()
def engine(scratch_url):
    e = create_engine(scratch_url)
    yield e
    e.dispose()


@pytest.fixture()
def session(engine, clean_tenants):
    # Requesting clean_tenants here makes it set up first and torn down last, so this
    # session is closed before TRUNCATE runs. A session left "idle in transaction" by a
    # SELECT holds a lock that would otherwise block the TRUNCATE forever.
    with Session(engine) as s:
        yield s


@pytest.fixture()
def clean_tenants(engine):
    """Empty Onto tables before and after a test that registers tenants."""
    def wipe():
        with engine.begin() as c:
            c.execute(text("SET LOCAL lock_timeout = '10s'"))  # a leaked transaction fails, never hangs
            c.execute(text(
                "TRUNCATE onto_drift, onto_node, onto_edge, onto_proposal, onto_release_current, onto_release, job_run, "
                "odoo_tick, decision_log, llm_usage, eval_run, tenant CASCADE"
            ))
    wipe()
    yield
    wipe()


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    """Run `async def` tests without a plugin (the repo has no pytest-asyncio)."""
    import asyncio
    import inspect

    if inspect.iscoroutinefunction(pyfuncitem.obj):
        args = {a: pyfuncitem.funcargs[a] for a in pyfuncitem._fixtureinfo.argnames}
        asyncio.run(pyfuncitem.obj(**args))
        return True
