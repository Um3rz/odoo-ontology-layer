# -*- coding: utf-8 -*-
"""Installation and ownership transfer for the combined Native BI addon."""

from odoo import SUPERUSER_ID, api

from .models.onto_token import ensure_token_secret

# Read groups the Onto bot needs on the apps it reads. Each one is granted only
# when its module is installed, so this addon does not force those apps on.
BOT_READ_GROUP_XMLIDS = [
    "stock.group_stock_user",
    "purchase.group_purchase_user",
    "sales_team.group_sale_salesman",
    "point_of_sale.group_pos_user",
]


def grant_onto_bot_groups(env):
    """Give the Onto AI bot read groups for the apps that are installed. Idempotent."""
    bot = env.ref("odoo_dashboards_bi.user_onto_bot", raise_if_not_found=False)
    if not bot:
        return
    groups = [env.ref(xmlid, raise_if_not_found=False) for xmlid in BOT_READ_GROUP_XMLIDS]
    bot.write({"groups_id": [(4, group.id) for group in groups if group]})


def post_init_hook(cr, registry):
    env = api.Environment(cr, SUPERUSER_ID, {})
    ensure_token_secret(env)
    grant_onto_bot_groups(env)


def migrate_agents_addon(cr):
    """Bind BI's XML IDs to the existing records before Odoo loads its data.

    Retire the old module registration without running an uninstall. Old XML IDs
    remain aliases so existing integrations can resolve the same records.
    """
    old = "odoo_dashboards_agents"
    new = "odoo_dashboards_bi"
    cr.execute("SELECT state FROM ir_module_module WHERE name = %s", (old,))
    row = cr.fetchone()
    if not row or row[0] in ("uninstalled", "uninstallable"):
        return

    cr.execute("""
        SELECT legacy.name
          FROM ir_model_data legacy
          JOIN ir_model_data current ON current.module = %s AND current.name = legacy.name
         WHERE legacy.module = %s
           AND (current.model, current.res_id) IS DISTINCT FROM (legacy.model, legacy.res_id)
         LIMIT 1
    """, (new, old))
    conflict = cr.fetchone()
    if conflict:
        raise RuntimeError("Native BI cannot take ownership of conflicting XML ID %s" % conflict[0])

    cr.execute("""
        INSERT INTO ir_model_data (module, name, model, res_id, noupdate)
        SELECT %s, name, model, res_id, noupdate FROM ir_model_data WHERE module = %s
        ON CONFLICT (module, name) DO NOTHING
    """, (new, old))
    cr.execute("UPDATE ir_model_data SET noupdate = true WHERE module = %s", (old,))
    cr.execute("UPDATE ir_module_module SET state = 'uninstalled' WHERE name = %s", (old,))
