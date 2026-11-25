"""Migrations on a fresh database (plan 0.6 Done-when). The scratch database is
created by the `scratch_url` fixture and dropped afterwards."""
import os
import subprocess
import sys

from sqlalchemy import inspect

from tests.onto.conftest import ROOT

EXPECTED = {
    "tenant", "onto_node", "onto_edge", "onto_proposal", "onto_release", "onto_release_current",
    "job_run", "odoo_tick", "decision_log", "llm_usage", "eval_run", "onto_drift",
}


def test_fresh_database_gets_every_onto_table(engine):
    assert EXPECTED <= set(inspect(engine).get_table_names())


def test_every_table_has_tenant_fk_and_timestamps(engine):
    insp = inspect(engine)
    for t in EXPECTED - {"tenant"}:
        cols = {c["name"]: c for c in insp.get_columns(t)}
        assert {"tenant_id", "created_at", "updated_at"} <= set(cols), t
        assert not cols["tenant_id"]["nullable"], t
        fks = [fk for fk in insp.get_foreign_keys(t) if fk["constrained_columns"] == ["tenant_id"]]
        assert fks and fks[0]["referred_table"] == "tenant", t
    tenant_cols = {c["name"] for c in insp.get_columns("tenant")}
    assert {"odoo_base_url", "db_uuid", "created_at", "updated_at"} <= tenant_cols


def test_contract_3_4_columns(engine):
    insp = inspect(engine)
    want = {
        "onto_node": {"release_id", "key", "kind", "payload", "status", "provenance"},
        "onto_edge": {"release_id", "src_key", "dst_key", "kind", "payload"},
        "onto_proposal": {"node_key", "payload", "confidence", "validation", "state", "approver_uid", "approved_at"},
        "onto_release": {"hash", "created_by_uid", "published_at"},
        "job_run": {"job", "state", "heartbeat_at", "result"},
        "odoo_tick": {"at", "module_version", "release", "steps", "lag_s"},
        "decision_log": {"run_id", "decision_type", "option_ids", "chosen", "confidence", "provider", "ms", "agreed_with_claude"},
        "llm_usage": {"day", "provider", "model", "input_tokens", "output_tokens", "cost"},
    }
    for t, cols in want.items():
        assert cols <= {c["name"] for c in insp.get_columns(t)}, t


def test_release_hash_is_unique_per_tenant(engine):
    uniques = inspect(engine).get_unique_constraints("onto_release")
    assert any(set(u["column_names"]) == {"tenant_id", "hash"} for u in uniques)


def test_no_table_stores_question_or_answer_text(engine):
    banned = {"question", "answer", "prompt", "text", "message", "rows", "body"}
    insp = inspect(engine)
    for t in EXPECTED:
        assert not banned & {c["name"] for c in insp.get_columns(t)}, t


def test_downgrade_and_upgrade_again(scratch_url, engine):
    env = {**os.environ, "DATABASE_URL": scratch_url}

    def alembic(*args):
        r = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env,
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-1500:]

    alembic("downgrade", "base")
    assert "tenant" not in inspect(engine).get_table_names()
    alembic("upgrade", "head")
    assert EXPECTED <= set(inspect(engine).get_table_names())
