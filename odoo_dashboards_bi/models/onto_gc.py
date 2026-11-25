# -*- coding: utf-8 -*-
"""Onto retention (3.11, plan A5.7): the nightly sweep that keeps Onto's rows in the client's Odoo small.

Each category is a registry entry (name, build, act) from _onto_gc_categories(). build(env, now) returns the model
and domain of the expired rows; act(batch) deletes or blanks one batch and returns the counts per model. The run
goes through the categories in order, one batch of BATCH rows per committed transaction, until the 30 s budget is
spent; then it triggers its own cron again. Each category reached writes one onto.audit row per run (counts only).

Periods are ir.config_parameter values (Settings -> Data retention). A missing or invalid value takes the default,
and no period goes below one day. Only Onto's own rows are touched (plan A5.7).

ponytail: no token jti category: onto/p3-odoo puts a jti in each token and stores none, so there is nothing to sweep.
"""

import json
import logging
import time
from datetime import timedelta

from odoo import api, models

from .onto_agent_run import LOG_KINDS
from .onto_store import onto_now

_logger = logging.getLogger(__name__)

BATCH = 5000
BUDGET_SECONDS = 30  # the cron's budget (plan 6.1); a batch that starts inside it may end a little after
STATEMENT_TIMEOUT_MS = 10000  # one batch's transaction
CRON_XMLID = "odoo_dashboards_bi.ir_cron_onto_gc"
PARAM_PREFIX = "onto.retention."  # ir.config_parameter key = PARAM_PREFIX + period name
PERIODS = {"chat_days": 90, "run_trace_days": 30, "run_summary_days": 180, "generation_log_days": 180}


def period_days(env, name):
    """Days a period keeps rows: its ir.config_parameter value, or the default when unset or not a whole number."""
    raw = env["ir.config_parameter"].sudo().get_param(PARAM_PREFIX + name) or ""
    try:
        return max(1, int(raw))
    except ValueError:
        return PERIODS[name]


def _cutoff(env, now, name):
    return now - timedelta(days=period_days(env, name))


def _agent_runs(env, now):
    return "onto.agent.run", [("create_date", "<", _cutoff(env, now, "run_summary_days"))]


def _agent_traces(env, now):
    return "onto.agent.run", [("create_date", "<", _cutoff(env, now, "run_trace_days")), ("trace", "!=", False)]


def _chat_threads(env, now):
    cutoff = _cutoff(env, now, "chat_days")
    # 90 days after the last message; a thread that never got one is judged by when it was created.
    return "onto.chat.thread", [
        "|", ("last_message_at", "<", cutoff), "&", ("last_message_at", "=", False), ("create_date", "<", cutoff),
    ]


def _generation_logs(env, now):
    return "ai.generation.log.saas", [
        ("kind", "in", list(LOG_KINDS.values())), ("create_date", "<", _cutoff(env, now, "generation_log_days")),
    ]


def _delete(batch):
    # ponytail: ORM unlink (cascades run in Python); raw DELETE per table if a batch nears the statement timeout.
    count = len(batch)
    batch.unlink()
    return {batch._name: count}


def _blank_traces(batch):
    count = len(batch)
    batch.write({"trace": False})
    return {batch._name: count}


def _delete_threads(batch):
    messages = batch.env["onto.chat.message"].search([("thread_id", "in", batch.ids)])
    counts = {"onto.chat.thread": len(batch), "onto.chat.message": len(messages)}
    messages.unlink()
    batch.unlink()
    return counts


class OntoGc(models.AbstractModel):
    _name = "onto.gc"
    _description = "Onto data retention (plan A5.7)"

    @api.model
    def _onto_gc_cron(self):
        """Daily cron (data/onto_cron.xml)."""
        self._onto_gc_run(BUDGET_SECONDS)

    @api.model
    def _onto_gc_categories(self):
        """Run order. A later phase adds (name, build, act) tuples by overriding this and calling super()."""
        return [
            ("agent_run", _agent_runs, _delete),
            ("agent_trace", _agent_traces, _blank_traces),
            ("chat_thread", _chat_threads, _delete_threads),
            ("generation_log", _generation_logs, _delete),
        ]

    @api.model
    def _onto_gc_run(self, budget):
        """Run the categories in order until the budget is spent. Returns {category: counts}. Triggers the cron again
        when work is left, and writes one onto.audit row for each category it reached."""
        self = self.sudo()
        started, now = time.monotonic(), onto_now()
        results, left = {}, False
        for name, build, act in self._onto_gc_categories():
            if time.monotonic() - started >= budget:
                left = True
                break
            model_name, domain = build(self.env, now)
            counts, partial = self._onto_gc_category(model_name, domain, act, started, budget)
            results[name] = counts
            self.env["onto.audit"].create({
                "action": "onto_gc.%s" % name,
                "detail": json.dumps({"counts": counts, "partial": partial}, sort_keys=True),
            })
            self.env.cr.commit()
            if partial:
                left = True
                break
        if left:
            self.env.ref(CRON_XMLID)._trigger()
        _logger.info("Onto retention: %s in %.1f s, more left: %s", results, time.monotonic() - started, left)
        return results

    def _onto_gc_category(self, model_name, domain, act, started, budget):
        """Act on the expired rows one committed batch at a time. Returns (counts per model, stopped by the budget)."""
        counts = {model_name: 0}
        while True:
            if time.monotonic() - started >= budget:
                return counts, True
            self.env.cr.execute("SET LOCAL statement_timeout = %s", (STATEMENT_TIMEOUT_MS,))
            batch = self.env[model_name].search(domain, limit=BATCH, order="id")
            last = len(batch) < BATCH
            for model, count in (act(batch) if batch else {}).items():
                counts[model] = counts.get(model, 0) + count
            self.env.cr.commit()
            if last:
                return counts, False
