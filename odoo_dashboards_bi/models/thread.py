import logging
import uuid
import time
from datetime import timedelta

import psycopg2

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError, UserError

from odoo.addons.odoo_dashboards_bi.models.onto_query import QuerygenError, querygen_call
from odoo.addons.odoo_dashboards_saas.models.ai_governance import charge_ai_credits

from . import evidence


_logger = logging.getLogger(__name__)
ACTIVE = ("queued", "running")
MAX_STEPS = 12
MAX_TRIES = 3
MAX_AGE = timedelta(minutes=15)
USAGE_COUNTERS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "credits")


def _accumulate_usage(previous, current):
    result = dict(previous or {})
    if isinstance(current.get("model"), str):
        result["model"] = current["model"]
    for key in USAGE_COUNTERS:
        value = current.get(key)
        if type(value) in (int, float) and 0 <= value < 10 ** 9:
            result[key] = result.get(key, 0) + value
    return result


def _trace_source(tool, source):
    args = tool.get("arguments") or {}
    label = source.get("label") or source.get("definition")
    return {
        "tool": tool.get("name"), "kind": source.get("source_kind"),
        "label": label if isinstance(label, str) else source.get("source_id"),
        "reference": source.get("source_id"), "metric": args.get("metric"),
        "period": source.get("period"), "dimensions": source.get("dims"),
        "release": source.get("release"), "row_count": len(source.get("rows") or []),
    }


class NativeBiThread(models.Model):
    _name = "native.bi.thread"
    _description = "BI conversation"
    _order = "write_date desc, id desc"

    name = fields.Char(required=True, default="New conversation")
    user_id = fields.Many2one("res.users", required=True, ondelete="cascade", index=True)

    @api.model
    def _internal(self):
        if not self.env.user.active or not self.env.user.has_group("base.group_user"):
            raise AccessError(_("BI requires an internal Odoo account."))

    @api.model
    def _own(self, thread_id):
        self._internal()
        if type(thread_id) is not int:
            raise AccessError(_("Conversation unavailable."))
        thread = self.search([("id", "=", thread_id), ("user_id", "=", self.env.uid)], limit=1)
        if not thread:
            raise AccessError(_("Conversation unavailable."))
        return thread.sudo()

    @api.model
    def _own_turn(self, turn_id):
        self._internal()
        if type(turn_id) is not int:
            raise AccessError(_("Turn unavailable."))
        turn = self.env["native.bi.turn"].sudo().search([
            ("id", "=", turn_id), ("thread_id.user_id", "=", self.env.uid),
        ], limit=1)
        if not turn:
            raise AccessError(_("Turn unavailable."))
        return turn

    @api.model
    def list_threads(self):
        self._internal()
        return [{"id": thread.id, "title": thread.name} for thread in self.search([
            ("user_id", "=", self.env.uid),
        ], limit=50)]

    @api.model
    def create_thread(self):
        self._internal()
        thread = self.sudo().create({"user_id": self.env.uid})
        return {"id": thread.id, "title": thread.name, "messages": [], "active_turn": False}

    @api.model
    def get_thread(self, thread_id):
        thread = self._own(thread_id)
        turns = self.env["native.bi.turn"].sudo().search([("thread_id", "=", thread.id)], limit=40)
        trace_by_turn = {}
        for event in self.env["native.bi.event"].sudo().search([
            ("turn_id", "in", turns.ids), ("kind", "=", "source"),
        ], order="sequence,id"):
            trace_by_turn.setdefault(event.turn_id.id, []).append(event.data)
        messages, active = [], False
        for turn in reversed(turns):
            messages.append({"role": "user", "text": turn.question})
            if turn.status in ACTIVE:
                active = {"id": turn.id, "status": turn.status}
            elif turn.card and (shown := turn._can_show_card(self.env)):
                messages.append({"role": "assistant", "text": turn.card.get("text", ""), "card": turn.card,
                                 "sources_used": trace_by_turn.get(turn.id, []), "stale": shown == "stale"})
            elif turn.status in ("failed", "cancelled"):
                messages.append({"role": "assistant", "text": turn.error})
            elif turn.card:
                messages.append({"role": "assistant", "text": _("The saved answer could not be verified against your current data or access. Ask again to refresh it.")})
        return {"id": thread.id, "title": thread.name, "messages": messages, "active_turn": active}

    @api.model
    def start_turn(self, thread_id, question, client_id, context=None):
        thread = self._own(thread_id)
        if not isinstance(question, str) or not 0 < len(question.strip()) <= 4000:
            raise UserError(_("Enter a question between 1 and 4,000 characters."))
        if not isinstance(client_id, str) or not 0 < len(client_id) <= 80:
            raise UserError(_("The request identifier is invalid."))
        companies = self.env.companies.ids
        evidence.check_enabled(self.env, companies)
        attachment = evidence.attachment(context)
        self.env.cr.execute("SELECT id FROM native_bi_thread WHERE id = %s FOR UPDATE", (thread.id,))
        Turn = self.env["native.bi.turn"].sudo()
        prior = Turn.search([("thread_id", "=", thread.id), ("client_id", "=", client_id)], limit=1)
        if prior:
            return {"id": prior.id, "status": prior.status}
        if Turn.search_count([("thread_id", "=", thread.id), ("status", "in", ACTIVE)]):
            raise UserError(_("Wait for the current question to finish."))
        turn = Turn.create({
            "thread_id": thread.id, "client_id": client_id, "run_key": str(uuid.uuid4()),
            "question": question.strip(), "attachment": attachment, "company_ids_json": companies,
            "scope": evidence.scope_key(self.env),
        })
        thread.name = question.strip()[:95] if thread.name == "New conversation" else thread.name
        turn._emit("queued", {"text": _("Question queued.")})
        self.env.ref("odoo_dashboards_bi.native_bi_turn_cron").sudo()._trigger()
        return {"id": turn.id, "status": turn.status}

    @api.model
    def events_after(self, turn_id, sequence=0):
        turn = self._own_turn(turn_id)
        sequence = max(0, sequence) if type(sequence) is int else 0
        events = self.env["native.bi.event"].sudo().search([
            ("turn_id", "=", turn.id), ("sequence", ">", sequence),
        ], order="sequence", limit=75)
        can_show_sources = not events.filtered(lambda item: item.kind == "source") or turn._can_show_card(self.env)
        payload = []
        for item in events:
            is_private = item.kind == "answer" or (item.kind == "source" and not can_show_sources)
            payload.append({"sequence": item.sequence, "kind": item.kind,
                            "data": {} if is_private else (item.data or {})})
        return {"status": turn.status, "events": payload}

    @api.model
    def cancel_turn(self, turn_id):
        turn = self._own_turn(turn_id)
        self.env.cr.execute("SELECT id FROM native_bi_turn WHERE id = %s FOR UPDATE", (turn.id,))
        turn.invalidate_recordset(["status"])
        if turn.status in ACTIVE:
            turn._finish("cancelled", _("Question cancelled."))
        return {"id": turn.id, "status": turn.status}


class NativeBiTurn(models.Model):
    _name = "native.bi.turn"
    _description = "BI turn"
    _order = "id desc"

    thread_id = fields.Many2one("native.bi.thread", required=True, ondelete="cascade", index=True)
    client_id = fields.Char(required=True)
    run_key = fields.Char(required=True)
    question = fields.Text(required=True)
    attachment = fields.Json()
    company_ids_json = fields.Json(required=True)
    scope = fields.Char(required=True)
    catalog = fields.Json()
    state_json = fields.Json()
    tool_results = fields.Json()
    proofs = fields.Json()
    usage = fields.Json()
    card = fields.Json()
    step = fields.Integer()
    tries = fields.Integer()
    sequence = fields.Integer()
    retry_at = fields.Datetime()
    status = fields.Selection(
        [(item, item.title()) for item in (*ACTIVE, "done", "refused", "cancelled", "failed")],
        default="queued", required=True, index=True,
    )
    error = fields.Char()
    _sql_constraints = [
        ("client_unique", "unique(thread_id, client_id)", "This question was already submitted."),
        ("run_unique", "unique(run_key)", "This run already exists."),
    ]

    def _emit(self, kind, data):
        self.ensure_one()
        self.sequence += 1
        self.env["native.bi.event"].sudo().create({
            "turn_id": self.id, "sequence": self.sequence, "kind": kind, "data": data,
        })
        # The browser retrieves cards under current access via RPC. Bus notifications carry no evidence.
        message = {"turn_id": self.id, "sequence": self.sequence, "kind": kind, "status": self.status}
        self.env["bus.bus"]._sendone(self.thread_id.user_id.partner_id, "native_bi.progress", message)

    def _text_stream(self):
        """(on_delta, flush): the answer text goes to the owner's browser as the model writes it. The turn's own
        transaction commits only when the step ends, so each chunk is sent from a separate cursor, a few per second.
        The final card still arrives through the normal access-checked RPC and replaces this text."""
        partner_id, turn_id = self.thread_id.user_id.partner_id.id, self.id
        buffer, sent = [], [time.monotonic()]

        def send(text, reset=False):
            with self.pool.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                env["bus.bus"]._sendone(env["res.partner"].browse(partner_id), "native_bi.delta",
                                        {"turn_id": turn_id, "text": text, "reset": reset})

        def on_delta(text):
            if text is None:  # querygen is rewriting the answer: clear what the browser shows
                buffer.clear()
                send("", reset=True)
                return
            buffer.append(text)
            if time.monotonic() - sent[0] >= 0.15:
                flush()

        def flush():
            if buffer:
                send("".join(buffer))
                buffer.clear()
            sent[0] = time.monotonic()

        send("", reset=True)  # a new step: drop text an earlier step showed
        return on_delta, flush

    def _finish(self, status, error=None, card=None):
        self.write({"status": status, "error": error, "card": card, "state_json": False, "tool_results": False})
        self._emit("answer" if card else status, {} if card else {"text": error})

    def _can_show_card(self, env):
        """False: hide the saved answer. True: its evidence is unchanged. "stale": same access as when it was
        answered, but the data (or how a query reads it) has changed since; it is shown with a note."""
        try:
            if self.company_ids_json != env.companies.ids:
                return False
            old_page = self._is_legacy_page_answer()
            scope = self.scope or ""
            same_access = scope == evidence.scope_key(env)
            # Older scopes (v2 held the user's write_date) cannot be compared: those answers stay only while
            # every query still returns identical evidence under current access.
            if not same_access and (scope.startswith("v3:") or (
                    not scope.startswith("v2:") and not old_page and scope != evidence.legacy_scope_key(env))):
                return False
            evidence.check_enabled(env, self.company_ids_json, check_credits=False)
            evidence.validate_references(env, self.attachment or {})
            with env.cr.savepoint():
                changed = self._proofs_changed(env, legacy_page=old_page, tolerate_errors=same_access)
        except (AccessError, UserError, psycopg2.Error):
            return False
        if not changed:
            return True
        # ponytail: same groups, rules and rights, so the user may see what they were shown before, even if a
        # data-level change (e.g. a reassigned salesperson) would now hide a row. Hide instead if that matters.
        return "stale" if same_access else False

    def _is_legacy_page_answer(self):
        if (self.scope or "").startswith("v2:") or not self.proofs:
            return False
        attachment = self.attachment or {}
        if attachment.get("kind") not in {"record", "view"} or self.proofs[0].get("tool"):
            return False
        return all(
            proof.get("tool", {}).get("name") in {"search_read", "read_records", "search_count", "read_group"}
            and proof["tool"].get("arguments", {}).get("model") == attachment.get("model")
            for proof in self.proofs[1:]
        )

    def _check_proofs(self, env):
        # Revocation can come from data as well as access configuration, e.g. a
        # salesperson changing on a record under a still-identical record rule.
        if self._proofs_changed(env):
            raise AccessError(_("Evidence or permissions changed. Run the question again."))

    def _proofs_changed(self, env, *, legacy_page=False, tolerate_errors=False):
        """Re-run each proof as the user. An AccessError always propagates. With tolerate_errors, a query
        that no longer runs (UserError, e.g. a metric renamed by an update) counts as changed evidence."""
        for item in self.proofs or []:
            tool = item.get("tool")
            try:
                if tool:
                    source = evidence.execute(env, self.catalog, self.attachment or {}, tool)
                else:
                    source = evidence.context_for_step(env, self.attachment or {})["evidence"]
            except AccessError:
                raise
            except UserError:
                if tolerate_errors:
                    return True
                raise
            if evidence.fingerprint(source) == item["fingerprint"]:
                continue
            if (legacy_page and (not tool or tool["name"] in {"search_read", "read_records"})
                    and evidence.legacy_page_fingerprint(source, tool) == item["fingerprint"]):
                continue
            return True
        return False

    @api.model
    def _cron_run(self):
        self.env.cr.execute(
            "SELECT id FROM native_bi_turn WHERE status IN ('queued', 'running') "
            "AND (retry_at IS NULL OR retry_at <= %s) ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1",
            (fields.Datetime.now(),),
        )
        row = self.env.cr.fetchone()
        if not row:
            return
        turn = self.sudo().browse(row[0])
        try:
            with self.env.cr.savepoint():
                turn._advance()
        except QuerygenError as exc:
            tries = turn.tries + 1
            if exc.status in (401, 403, 402):
                turn._finish("failed", _("This installation is not authorized for native BI in Querygen."))
            elif tries < MAX_TRIES and (exc.status is None or exc.status in (429, 500, 502, 503, 504)):
                turn.write({"tries": tries, "retry_at": fields.Datetime.now() + timedelta(seconds=10 * tries)})
                turn._emit("progress", {"text": _("Querygen is unavailable. Retrying this step.")})
            else:
                turn._finish("failed", _("The BI service is unavailable. Try asking again."))
        except (AccessError, UserError) as exc:
            turn._finish("failed", str(exc))
        except Exception:
            _logger.exception("Native BI turn failed id=%s", turn.id)
            turn._finish("failed", _("The question could not be completed."))
        if turn.status in ACTIVE and not turn.retry_at:
            self.env.ref("odoo_dashboards_bi.native_bi_turn_cron").sudo()._trigger()
        elif self.search_count([("status", "in", ACTIVE), ("retry_at", "=", False)]):
            self.env.ref("odoo_dashboards_bi.native_bi_turn_cron").sudo()._trigger()

    def _advance(self):
        self.ensure_one()
        if self.step >= MAX_STEPS or (self.create_date and fields.Datetime.now() - self.create_date > MAX_AGE):
            self._finish("failed", _("The analysis reached its step limit. Narrow the question and try again."))
            return
        user = self.thread_id.user_id
        env = api.Environment(self.env.cr, user.id, {
            "allowed_company_ids": self.company_ids_json, "lang": user.lang, "tz": user.tz,
        }, su=False)
        if not user.active or self.scope != evidence.scope_key(env):
            raise AccessError(_("Your data access changed. Start a new question."))
        evidence.check_enabled(env, self.company_ids_json)
        evidence.validate_references(env, self.attachment or {})
        self._check_proofs(env)
        payload = {"run_id": self.run_key, "step": self.step}
        if not self.step:
            self.catalog = evidence.catalog(env, self.attachment or {})
            context = evidence.context_for_step(env, self.attachment or {})
            if context["evidence"]:
                self.proofs = [{"fingerprint": evidence.fingerprint(context["evidence"])}]
                for source in context["evidence"]:
                    self._emit("source", _trace_source({"name": "attachment"}, source))
            prior = self.search([
                ("thread_id", "=", self.thread_id.id), ("id", "<", self.id), ("status", "=", "done"),
            ], limit=4)
            history = []
            for item in reversed(prior):
                history.append({"role": "user", "text": item.question})
                if item.card and item._can_show_card(env):
                    sources = [{
                        key: source[key] for key in ("source_id", "definition", "period", "filters", "release")
                        if key in source
                    } for source in (item.card.get("sources") or [])[:3]]
                    answer = item.card.get("text", "")
                    history.append({
                        "role": "assistant",
                        "text": answer[:1500] + (("\nPrevious source scope: " + str(sources)[:1000]) if sources else ""),
                    })
            payload.update(
                question=self.question,
                history=history,
                context=context,
                catalog=self.catalog,
            )
        else:
            payload.update(state=self.state_json, tool_results=self.tool_results or [])
        self.status = "running"
        on_delta, flush = self._text_stream()
        try:
            reply = querygen_call(env, "native-bi/v1/step", evidence.json_data(payload), timeout=45, on_delta=on_delta)
        finally:
            flush()
        if not isinstance(reply, dict):
            raise UserError(_("The BI service returned an invalid step."))
        self.tries, self.retry_at = 0, False
        self.usage = _accumulate_usage(self.usage, reply.get("usage") or {})
        kind = reply.get("status")
        if kind in ("done", "refused") and isinstance(reply.get("card"), dict):
            charge_ai_credits(env, self.usage, kind="native_bi", user=env.user)
            self._finish(kind, card=reply["card"])
            return
        tools = reply.get("tools")
        if kind != "tools" or not isinstance(tools, list) or not 0 < len(tools) <= 5 or not reply.get("state"):
            raise UserError(_("The BI service returned an invalid tool request."))
        proofs, results = list(self.proofs or []), []
        for tool in tools:
            call_id = tool["id"]
            if not isinstance(call_id, str) or not call_id or any(x["id"] == call_id for x in results):
                raise UserError(_("The BI service returned duplicate or invalid tool requests."))
            if tool.get("name") == "search_web":
                query = (tool.get("arguments") or {}).get("query")
                if not isinstance(query, str) or not query.strip() or len(query) > 240:
                    results.append({"id": call_id, "ok": False, "result": {"error": "Invalid web search query."}})
                    continue
                try:
                    found = querygen_call(env, "native-bi/v1/web-search", {"query": query}, timeout=45)
                except QuerygenError:
                    results.append({"id": call_id, "ok": False, "result": {"error": "Web search unavailable."}})
                    continue
                self.usage = _accumulate_usage(self.usage, found.get("usage") or {})
                sources = found.get("sources") or []
                rows = [[found.get("summary", "")]]
                for source in sources:
                    value = {"source_kind": "web", "source_id": source["url"], "url": source["url"],
                             "label": source["title"], "columns": [{"key": "summary", "label": "Summary"}],
                             "rows": rows, "trust": "uncertified"}
                    self._emit("source", _trace_source(tool, value))
                if sources:
                    value.update(source_id=sources[0]["url"], url=sources[0]["url"],
                                 label=sources[0]["title"], references=sources)
                    results.append({"id": call_id, "ok": True, "result": value})
                else:
                    results.append({"id": call_id, "ok": False, "result": {"error": "No cited sources found."}})
                continue
            try:
                with self.env.cr.savepoint():
                    value = evidence.execute(env, self.catalog, self.attachment or {}, tool)
            except (AccessError, UserError) as exc:  # a refused call is a failed tool result the model can work around
                results.append({"id": call_id, "ok": False, "result": {"error": str(exc)}})
            else:
                proofs.append({"tool": tool, "fingerprint": evidence.fingerprint(value)})
                results.append({"id": call_id, "ok": True, "result": value})
                self._emit("source", _trace_source(tool, value))
        self.write({
            "step": self.step + 1, "state_json": reply["state"],
            "tool_results": results, "proofs": proofs,
        })
        self._emit("progress", {"text": _("Analyzed an Odoo data source.")})


class NativeBiEvent(models.Model):
    _name = "native.bi.event"
    _description = "BI progress event"

    turn_id = fields.Many2one("native.bi.turn", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(required=True)
    kind = fields.Char(required=True)
    data = fields.Json()
