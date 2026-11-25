# -*- coding: utf-8 -*-
"""Typed executors for approved recommendation options (3.7 skeleton; contract 3.8).

An executor runs as the approver (env.user, never sudo) inside the approval's savepoint, after the
approval's audit row. Any refusal ends the approval with 422 action_failed and Odoo's own message, and
nothing it did is kept. Only the types in CHECKS can be proposed or approved; 6.3 adds the rest.
"""

import json

from odoo import _, fields
from odoo.exceptions import AccessError, UserError, ValidationError

from .onto_store import StoreError, onto_now

ACTIVITY_KEYS = frozenset(("res_model", "res_id", "summary", "note", "date_deadline"))
# Fields an approver may change on a proposed option (allowlist; anything else is 422 limit).
MODIFIABLE = {"activity": frozenset(("summary", "note", "date_deadline"))}


def _bad():
    return StoreError(422, "bad_line")


def _parse_date(text):
    try:
        return fields.Date.to_date(text)
    except (TypeError, ValueError):
        return None


def _check_activity(params):
    if not set(params) <= ACTIVITY_KEYS:
        raise _bad()
    if not isinstance(params.get("res_model"), str) or not params["res_model"]:
        raise _bad()
    if type(params.get("res_id")) is not int:
        raise _bad()
    summary = params.get("summary")
    if not isinstance(summary, str) or not 0 < len(summary) <= 255:
        raise _bad()
    if params.get("note") is not None and not isinstance(params["note"], str):
        raise _bad()
    deadline = params.get("date_deadline")
    if deadline is not None and (not isinstance(deadline, str) or _parse_date(deadline) is None):
        raise _bad()


def _run_activity(env, rec, params):
    """activity.create: an activity on the target record, assigned to the approver."""
    if params["res_model"] not in env:
        raise UserError(_("The model of this activity is not available."))
    target = env[params["res_model"]].browse(params["res_id"]).exists()
    if not target:
        raise UserError(_("The record for this activity no longer exists."))
    target.check_access_rights("read")
    target.check_access_rule("read")
    env["onto.audit"].create(
        {
            "action": "onto.action.activity.create",
            "res_model": rec._name,
            "res_id": rec.id,
            "detail": "target=%s:%d" % (target._name, target.id),
        }
    )
    deadline = _parse_date(params["date_deadline"]) if params.get("date_deadline") else onto_now().date()
    env["mail.activity"].create(
        {
            "res_model_id": env["ir.model"]._get(target._name).id,
            "res_id": target.id,
            "summary": params["summary"],
            "note": params.get("note") or False,
            "date_deadline": deadline,
            "user_id": env.uid,
        }
    )


CHECKS = {"activity": _check_activity}
RUNS = {"activity": _run_activity}
ACTION_TYPES = frozenset(CHECKS)


def validate(action_type, params):
    """The shape of one option's params, at proposal and after a modification. Raises StoreError 422 bad_line."""
    if not isinstance(action_type, str) or action_type not in CHECKS or not isinstance(params, dict):
        raise _bad()
    CHECKS[action_type](params)


def modification(action_type, changes):
    """The allowlisted shallow patch an approver may apply to one option. Raises StoreError 422."""
    if not isinstance(changes, dict) or not changes:
        raise _bad()
    allowed = MODIFIABLE.get(action_type, frozenset())
    for name in sorted(changes):
        if name not in allowed:
            raise StoreError(422, "limit", message=name)
    return changes


def execute(env, rec, line):
    """Carries out one approved action option as env.user. A refusal becomes 422 action_failed."""
    params = json.loads(line.params or "{}")
    try:
        RUNS[line.action_type](env, rec, params)
    except (AccessError, UserError, ValidationError) as exc:
        raise StoreError(422, "action_failed", message=str(exc.args[0]) if exc.args else "") from exc
