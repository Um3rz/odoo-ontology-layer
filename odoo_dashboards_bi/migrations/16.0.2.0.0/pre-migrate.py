from odoo.addons.odoo_dashboards_bi.hooks import migrate_agents_addon


def migrate(cr, version):
    if version:
        migrate_agents_addon(cr)
