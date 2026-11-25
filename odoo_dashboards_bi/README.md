# Odoo Dashboards BI

The addon owns Native BI chat, ontology setup, and the supporting `onto.*` models, queries, routes, groups and record rules.

## Boundary

- `odoo_dashboards_saas` is the shared auth and API foundation: mobile API-key login, sealed transport and the `external_route` pattern. Access is not a dependency.
- Dashboards (`odoo_dashboards_saas`) remains a dependency. Home is optional: when installed, BI provides the `bi-ai` home page and follows the Home Style theme on the drawer. The consolidated addon also declares the existing Mail, Bus and Stock dependencies.
- Future Odoo work for `onto_plan.md` lands here, never in Saas.

## Capability areas (one addon)

Inventory, Forecasting, Planning and BI services live inside this addon.

| Area | New code goes in | Current files (not restructured) |
|---|---|---|
| Inventory | `models/onto_inventory_*.py`, `tests/test_onto_inventory_*.py` | `onto_stock_daily`, `onto_health`, `onto_org_map`, `onto_provision`, `onto_dataset` |
| Forecasting | `models/onto_forecast_*.py`, `tests/test_onto_forecast_*.py` | `onto_results` (forecast model) |
| Planning | `models/onto_planning_*.py`, `tests/test_onto_planning_*.py` | `onto_recommendation`, `onto_recommendation_line`, `onto_actions` |
| BI | `models/onto_bi_*.py`, `tests/test_onto_bi_*.py` | `onto_explore`, `onto_query`, `onto_agent_run`, `onto_chat` |

Shared plumbing stays where it is: `onto_store`, `onto_token`, `onto_rules`, `onto_scope`, `onto_jobs`, `onto_gc`, `onto_bot`, `controllers/onto.py`, `controllers/onto_auth.py`. Routes for a new area go in `controllers/onto.py` (or `controllers/onto_<area>.py` once it grows), using `onto_route` / `onto_external_route` only.

## Conventions

- Model names `onto.*`; route paths `/onto/v1/*`; files prefixed `onto_`; XML ids prefixed `onto_` or `group_onto_`.
- Groups, bot user and crons are owned by this addon (`odoo_dashboards_bi.*`). Refer to Saas records only by their `odoo_dashboards_saas.*` ids.
- Do not add a module-level dependency on an app (purchase, point_of_sale, sale, account) unless the code requires it unconditionally; guard optional apps at runtime as `onto_health` and `onto_introspect` do.

## Upgrade from the split addons

Back up the Odoo database and upgrade `odoo_dashboards_bi` to 16.0.2.0.0. The pre-upgrade migration binds the new XML IDs to existing records, preserves the old IDs for compatibility, and retires the old addon registration without running its uninstall routine. The published release, datasets, chat history and role assignments stay in the same database. To roll back, restore the pre-upgrade database backup and the previous code together.
