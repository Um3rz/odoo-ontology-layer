"""Read of Odoo's Onto agent settings (plan 3.6). Read-only, through the store read route (contract 3.3).

The kill switch is `agents_paused` on onto.agent.setting, one row per company. The company is the token's
`active_company_id` from whoami, the same company whose `ai_enabled` whoami reports (browser_auth). No row for
that company means the switch is off, since it defaults off. `ai_enabled` is a separate switch. Any read error
or non-boolean value raises OdooError, and the caller treats that as "paused" (fail closed).
"""
from app.odoo.client import OdooClient, OdooError

SETTING_MODEL = "onto.agent.setting"
READ_PATH = "/onto/v1/store/read"


async def kill_switch_on(odoo: OdooClient) -> bool:
    """True if the agents are paused for the token's company. Raises OdooError when it cannot be read."""
    cid = (await odoo.whoami()).get("active_company_id")
    if type(cid) is not int:
        raise OdooError(0, "active company unreadable")
    rows = await odoo.request("POST", READ_PATH, json={
        "model": SETTING_MODEL, "fields": ["company_id", "ai_enabled", "agents_paused"],
        "domain": [["company_id", "=", cid]], "limit": 1,
    }, want=list)
    if not all(isinstance(r, dict) and type(r.get("agents_paused")) is bool for r in rows):
        raise OdooError(0, "agent setting unreadable")
    return any(r["agents_paused"] for r in rows)
