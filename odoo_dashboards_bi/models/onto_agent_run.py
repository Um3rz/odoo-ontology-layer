# -*- coding: utf-8 -*-
"""Onto agent-run records and their AI ledger rows (contract 3.10).

/onto/v1/agent/record stores one finished agent turn through record_turn(): the run, its
chat messages, one ai.generation.log.saas row and the credit charge, all in the caller's
transaction. A query token writes its own user's chat turns; a bot token writes system
runs (playbook, job), which only stewards also see (security/onto_security.xml).
"""

import json

from odoo import api, fields, models

from odoo.addons.odoo_dashboards_saas.models.ai_governance import charge_ai_credits

from .onto_store import onto_now

KINDS = ("chat", "playbook", "job")
# ai.generation.log.saas kind per run kind (contract 3.10).
LOG_KINDS = {"chat": "onto_chat", "playbook": "onto_agent", "job": "onto_job"}
RUN_STATES = ("done", "failed", "cancelled")
MAX_TURN_MESSAGES = 20
PROMPT_CAP = 8000
PAYLOAD_CAP = 64000


class RecordError(Exception):
    """A refused agent record. status and code are what the route answers."""

    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code


def _invalid():
    return RecordError(422, "invalid_body")


def _text(value, required=False):
    if value in (None, ""):
        if required:
            raise _invalid()
        return ""
    if not isinstance(value, str):
        raise _invalid()
    return value


def _count(value):
    if value is None:
        return 0
    if type(value) is not int or value < 0:
        raise _invalid()
    return value


def _credits(value):
    """None when absent: saas then charges its fallback, so an older querygen cannot make AI free."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise _invalid()
    return float(value)


def _ledger(usage):
    """The usage in the shape charge_ai_credits reads."""
    return {
        "model": _text(usage.get("model"))[:200],
        "input_tokens": _count(usage.get("input_tokens")),
        "output_tokens": _count(usage.get("output_tokens")),
        "cache_read_input_tokens": _count(usage.get("cache_read_input_tokens")),
        "cache_creation_input_tokens": _count(usage.get("cache_creation_input_tokens")),
        "credits": _credits(usage.get("credits")),
    }


def _messages(chat):
    raw = (chat or {}).get("messages") or []
    if not isinstance(raw, list) or len(raw) > MAX_TURN_MESSAGES:
        raise _invalid()
    messages = []
    for item in raw:
        if not isinstance(item, dict) or item.get("role") not in ("user", "assistant"):
            raise _invalid()
        card = item.get("card")
        if card is not None and not isinstance(card, dict):
            raise _invalid()
        messages.append({"role": item["role"], "text": _text(item.get("text")), "card": card})
    return messages


def _ms(step):
    value = step.get("ms")
    return value if type(value) is int and value >= 0 else 0


class OntoAiGenerationLog(models.Model):
    """Onto's kinds in the saas AI ledger. Odoo rejects a Selection value the field does not list."""

    _inherit = "ai.generation.log.saas"

    kind = fields.Selection(
        selection_add=[
            ("onto_chat", "Onto chat"),
            ("onto_agent", "Onto agent run"),
            ("onto_job", "Onto job"),
        ],
        ondelete={"onto_chat": "cascade", "onto_agent": "cascade", "onto_job": "cascade"},
    )


class OntoAgentRun(models.Model):
    _name = "onto.agent.run"
    _inherit = ["onto.mixin"]
    _description = "Onto agent run record"
    # Not unique on run_key: a LangGraph thread (the run id) has one record per turn.
    _sql_constraints = [
        ("onto_agent_run_idempotency_uniq", "unique(idempotency_key)",
         "Each agent/record turn is stored once."),
    ]

    run_key = fields.Char(required=True, index=True, help="LangGraph thread id (the run id).")
    idempotency_key = fields.Char(index=True, help="'<run id>:<turn>', so retries are safe.")
    user_id = fields.Many2one(
        "res.users", required=True, index=True, default=lambda self: self.env.user,
        help="The asking user, or the AI bot for system runs.",
    )
    kind = fields.Selection(
        [("chat", "Chat"), ("playbook", "Playbook"), ("job", "Job")], required=True
    )
    trigger = fields.Char()
    playbook = fields.Char()
    state = fields.Selection(
        [("done", "Done"), ("failed", "Failed"), ("cancelled", "Cancelled")], required=True
    )
    outcome = fields.Text(help="JSON outcome.")
    trace = fields.Text(help="JSON compact trace: tool names, timings, short summaries.")
    model_name = fields.Char()
    input_tokens = fields.Integer()
    output_tokens = fields.Integer()
    cache_read_tokens = fields.Integer()
    cache_creation_tokens = fields.Integer()
    credits = fields.Float(digits=(16, 4))
    recommendation_ids = fields.Many2many("onto.recommendation", string="Recommendations")
    chat_thread_id = fields.Many2one("onto.chat.thread", ondelete="set null", index=True)
    generation_log_id = fields.Many2one("ai.generation.log.saas", ondelete="set null", index=True)

    def _record_result(self):
        self.ensure_one()
        return {
            "thread_id": self.chat_thread_id.id or None,
            "run_id": self.id,
            "generation_log_id": self.generation_log_id.id or None,
        }

    @api.model
    def record_turn(self, caller, run, chat, usage, idempotency_key):
        """Store one finished agent turn (contract 3.10) as the caller. Returns the ids written, or the ids
        of the earlier write with the same idempotency key, which changes nothing.

        The run, the messages, the AI ledger row and the credit charge are written in the caller's
        transaction, so a failure leaves none of them behind."""
        if caller not in ("query", "bot"):
            raise RecordError(403, "token_kind")
        if not isinstance(run, dict) or (chat is not None and not isinstance(chat, dict)):
            raise _invalid()
        if usage is not None and not isinstance(usage, dict):
            raise _invalid()
        run_id = _text(run.get("id"), required=True)
        key = _text(idempotency_key, required=True)
        kind, state = run.get("kind"), run.get("state")
        if kind not in KINDS or state not in RUN_STATES or not key.startswith(run_id + ":"):
            raise _invalid()
        # A query token writes only its user's interactive chat; system runs come from the bot.
        if caller == "query" and kind != "chat":
            raise RecordError(403, "token_kind")
        if kind == "chat" and chat is None:
            raise _invalid()
        messages = _messages(chat)
        trace = run.get("trace") or []
        if not isinstance(trace, list) or not all(isinstance(step, dict) for step in trace):
            raise _invalid()
        rec_ids = run.get("recommendation_ids") or []
        if not isinstance(rec_ids, list) or not all(type(i) is int for i in rec_ids):
            raise _invalid()
        if len(self.env["onto.recommendation"].search([("id", "in", rec_ids)])) != len(set(rec_ids)):
            raise RecordError(422, "unknown_recommendation")
        ledger = _ledger(usage or {})

        existing = self.search([("idempotency_key", "=", key)], limit=1)
        if existing:
            return existing._record_result()
        if self.sudo().search_count([("idempotency_key", "=", key)]):
            # Someone else's turn under this key: say so without changing anything.
            raise RecordError(409, "conflict")

        thread = self.env["onto.chat.thread"]
        thread_id = (chat or {}).get("thread_id")
        if thread_id is not None:
            if type(thread_id) is not int:
                raise _invalid()
            thread = self.env["onto.chat.thread"].search([("id", "=", thread_id)], limit=1)
            if not thread:
                raise RecordError(404, "not_found")
        elif messages:
            thread = self.env["onto.chat.thread"].create({
                "user_id": self.env.user.id,
                "title": _text((chat or {}).get("title"))[:255],
            })

        last_user = next((m["text"] for m in reversed(messages) if m["role"] == "user"), "")
        prompt = last_user or _text(run.get("playbook") or run.get("trigger")) or kind
        # The ledger keeps a summary only; the answer card lives on onto.chat.message.
        summary = json.dumps({"run": run_id, "kind": kind, "state": state, "messages": len(messages)})
        log = self.env["ai.generation.log.saas"].sudo().create({
            "kind": LOG_KINDS[kind],
            "status": "success" if state == "done" else "failed",
            "user_id": self.env.user.id,
            "prompt": prompt[:PROMPT_CAP],
            "payload": summary[:PAYLOAD_CAP],
            "error": False if state == "done" else state,
            "duration_ms": sum(_ms(step) for step in trace),
            "model_used": ledger["model"],
            "input_tokens": ledger["input_tokens"],
            "output_tokens": ledger["output_tokens"],
            "cache_read_tokens": ledger["cache_read_input_tokens"],
            "cache_write_tokens": ledger["cache_creation_input_tokens"],
            "credits": ledger["credits"] or 0.0,
        })
        run_rec = self.create({
            "run_key": run_id,
            "idempotency_key": key,
            "user_id": self.env.user.id,
            "kind": kind,
            "trigger": _text(run.get("trigger"))[:255],
            "playbook": _text(run.get("playbook"))[:255],
            "state": state,
            "outcome": json.dumps(run["outcome"]) if run.get("outcome") is not None else False,
            "trace": json.dumps(trace),
            "model_name": ledger["model"],
            "input_tokens": ledger["input_tokens"],
            "output_tokens": ledger["output_tokens"],
            "cache_read_tokens": ledger["cache_read_input_tokens"],
            "cache_creation_tokens": ledger["cache_creation_input_tokens"],
            "credits": ledger["credits"] or 0.0,
            "chat_thread_id": thread.id or False,
            "generation_log_id": log.id,
            "recommendation_ids": [(6, 0, rec_ids)],
        })
        if messages:
            self.env["onto.chat.message"].create([
                {
                    "thread_id": thread.id,
                    "role": m["role"],
                    "text": m["text"],
                    "card": json.dumps(m["card"]) if m["card"] is not None else False,
                    "run_id": run_rec.id,
                }
                for m in messages
            ])
            thread.write({"last_message_at": onto_now()})
        charge_ai_credits(self.env, ledger, kind=LOG_KINDS[kind], user=self.env.user, generation_log_id=log.id)
        return {"thread_id": thread.id or None, "run_id": run_rec.id, "generation_log_id": log.id}
