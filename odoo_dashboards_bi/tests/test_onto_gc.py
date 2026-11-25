# -*- coding: utf-8 -*-
"""Onto retention (3.11, plan A5.7). Written, not run: the canonical addon suite runs it at the phase end (plan 6.1).

The ORM sets create_date, so rows are backdated with SQL. BATCH is lowered so every category takes several batches.
Assertions name only the rows the test creates, because the database may hold other rows."""

import json
from datetime import timedelta
from unittest import mock

from odoo.tests.common import TransactionCase, tagged

from ..models import onto_gc
from ..models.onto_store import onto_now

TRACE = '[{"tool": "search", "ms": 12}]'


@tagged("post_install", "-at_install")
class TestOntoGc(TransactionCase):
    def setUp(self):
        super().setUp()
        now = onto_now()
        Run = self.env["onto.agent.run"]
        Thread = self.env["onto.chat.thread"]
        Log = self.env["ai.generation.log.saas"].sudo()
        # Agent runs: 200 d (deleted), 60 d (trace blanked, summary kept), 5 d (untouched).
        self.run_old = Run.create({"run_key": "old", "kind": "chat", "state": "done", "trace": TRACE})
        self.run_mid = Run.create({"run_key": "mid", "kind": "chat", "state": "done", "trace": TRACE,
                                   "outcome": '{"ok": true}'})
        self.run_new = Run.create({"run_key": "new", "kind": "chat", "state": "done", "trace": TRACE})
        self._backdate(self.run_old, 200)
        self._backdate(self.run_mid, 60)
        self._backdate(self.run_new, 5)
        # Chat threads: last message 100 d ago (deleted with its two messages), no message and created 100 d ago
        # (deleted), last message 10 d ago (untouched).
        self.thread_old = Thread.create({"title": "old", "last_message_at": now - timedelta(days=100)})
        self.thread_empty = Thread.create({"title": "never"})
        self._backdate(self.thread_empty, 100)
        self.thread_new = Thread.create({"title": "new", "last_message_at": now - timedelta(days=10)})
        Message = self.env["onto.chat.message"]
        self.old_messages = Message.create([
            {"thread_id": self.thread_old.id, "role": "user", "text": "a"},
            {"thread_id": self.thread_old.id, "role": "assistant", "text": "b"},
        ])
        self.new_message = Message.create({"thread_id": self.thread_new.id, "role": "user", "text": "c"})
        # Generation log: an Onto kind at 190 d (deleted), an Onto kind at 10 d and a non-Onto kind at 190 d (kept).
        self.log_old = Log.create({"kind": "onto_chat", "status": "success", "prompt": "q"})
        self.log_new = Log.create({"kind": "onto_chat", "status": "success", "prompt": "q"})
        self.log_other = Log.create({"kind": "viz", "status": "success", "prompt": "q"})
        self._backdate(self.log_old, 190)
        self._backdate(self.log_new, 10)
        self._backdate(self.log_other, 190)

    def _backdate(self, records, days):
        self.env.cr.execute(
            "UPDATE %s SET create_date = %%s WHERE id IN %%s" % records._table,
            [onto_now() - timedelta(days=days), tuple(records.ids)],
        )
        self.env.invalidate_all()

    def _run(self):
        """One full run with batches of two. Returns the audit rows it wrote."""
        last = self.env["onto.audit"].search([], order="id desc", limit=1).id or 0
        with mock.patch.object(onto_gc, "BATCH", 2):
            self.env["onto.gc"]._onto_gc_run(budget=600)
        self.env.invalidate_all()
        return self.env["onto.audit"].search([("id", ">", last)])

    def test_deletes_exactly_the_expired_rows(self):
        self._run()
        self.assertFalse(self.run_old.exists())
        self.assertTrue(self.run_mid.exists())
        self.assertFalse(self.run_mid.trace)
        self.assertEqual(self.run_mid.outcome, '{"ok": true}')
        self.assertTrue(self.run_new.trace)
        self.assertFalse(self.thread_old.exists())
        self.assertFalse(self.old_messages.exists())
        self.assertFalse(self.thread_empty.exists())
        self.assertTrue(self.thread_new.exists())
        self.assertTrue(self.new_message.exists())
        self.assertFalse(self.log_old.exists())
        self.assertTrue(self.log_new.exists())
        self.assertTrue(self.log_other.exists())

    def test_audit_counts_per_category(self):
        rows = self._run()
        self.assertEqual(len(rows), 4)
        detail = {row.action: json.loads(row.detail) for row in rows}
        self.assertEqual(detail, {
            "onto_gc.agent_run": {"counts": {"onto.agent.run": 1}, "partial": False},
            "onto_gc.agent_trace": {"counts": {"onto.agent.run": 1}, "partial": False},
            "onto_gc.chat_thread": {"counts": {"onto.chat.thread": 2, "onto.chat.message": 2}, "partial": False},
            "onto_gc.generation_log": {"counts": {"ai.generation.log.saas": 1}, "partial": False},
        })

    def test_period_defaults_and_overrides(self):
        params = self.env["ir.config_parameter"].sudo()
        self.assertEqual(onto_gc.period_days(self.env, "chat_days"), 90)
        params.set_param("onto.retention.chat_days", "45")
        self.assertEqual(onto_gc.period_days(self.env, "chat_days"), 45)
        params.set_param("onto.retention.chat_days", "soon")
        self.assertEqual(onto_gc.period_days(self.env, "chat_days"), 90)
        params.set_param("onto.retention.chat_days", "0")
        self.assertEqual(onto_gc.period_days(self.env, "chat_days"), 1)
