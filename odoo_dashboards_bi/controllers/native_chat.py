import json
import re

from odoo import http
from odoo.exceptions import AccessError
from odoo.http import request


def answer_chunks(text):
    words = re.findall(r"\S+\s*|\s+", text)
    for offset in range(0, len(words), 6):
        yield json.dumps({"text": "".join(words[offset:offset + 6])}, ensure_ascii=False) + "\n"


class NativeBiChat(http.Controller):
    @http.route("/native-bi/chat/<int:turn_id>/answer", type="http", auth="user", methods=["GET"])
    def answer(self, turn_id, companies=None):
        try:
            if companies:
                ids = [int(item) for item in companies.split(",")]
                if not 0 < len(ids) <= 100 or not set(ids).issubset(request.env.user.company_ids.ids):
                    return request.not_found()
                request.update_context(allowed_company_ids=ids)
            turn = request.env["native.bi.thread"]._own_turn(turn_id)
            if turn.status not in ("done", "refused") or not turn.card or not turn._can_show_card(request.env):
                return request.not_found()
        except (AccessError, ValueError):
            return request.not_found()
        text = turn.card.get("text") or ""
        return request.make_response(answer_chunks(text), headers=[
            ("Content-Type", "application/x-ndjson; charset=utf-8"),
            ("Cache-Control", "private, no-store"),
            ("X-Accel-Buffering", "no"),
        ])
