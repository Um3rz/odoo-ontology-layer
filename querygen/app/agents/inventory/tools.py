"""Tools the playbooks may call (A4.4). Each takes the run's Odoo client and typed args, and returns a small result.

Reads only: an inventory tool never writes. Rows come back as plain values and are dropped when the run ends.
"""
from typing import Awaitable, Callable

from app.odoo.client import OdooClient

STATUS_FIELDS = ["product_id", "location_id", "status", "refreshed_at"]
ROW_LIMIT = 20


class ToolArgError(ValueError):
    """The model sent arguments the tool does not accept."""


def _id(value):
    # Odoo returns many2one values as [id, name]; only the id is needed here.
    return value[0] if isinstance(value, list) and value else value


async def status_lookup(odoo: OdooClient, args: dict) -> dict:
    """Stock status rows for one product, optionally at one location (contract 3.3 store/read on onto.status)."""
    product_id, location_id = args.get("product_id"), args.get("location_id")
    if type(product_id) is not int or (location_id is not None and type(location_id) is not int):
        raise ToolArgError("product_id must be an int; location_id an int or null")
    domain = [["product_id", "=", product_id]]
    if location_id is not None:
        domain.append(["location_id", "=", location_id])
    rows = await odoo.request("POST", "/onto/v1/store/read", json={
        "model": "onto.status", "domain": domain, "fields": STATUS_FIELDS, "limit": ROW_LIMIT,
    }, want=list)
    return {"rows": [{
        "product_id": _id(r.get("product_id")), "location_id": _id(r.get("location_id")),
        "status": r.get("status"), "refreshed_at": r.get("refreshed_at"),
    } for r in rows if isinstance(r, dict)]}


TOOLS: dict[str, Callable[[OdooClient, dict], Awaitable[dict]]] = {"status_lookup": status_lookup}
