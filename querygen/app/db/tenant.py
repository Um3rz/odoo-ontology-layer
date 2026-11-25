"""The one tenant-filter helper. Every Onto query goes through `scoped`."""
import uuid

from sqlalchemy import Select
from sqlmodel import select


def scoped(model, tenant_id: uuid.UUID) -> Select:
    """`select(model)` limited to one tenant. Add further `.where()` as needed."""
    if not hasattr(model, "tenant_id"):
        raise TypeError(f"{model.__name__} has no tenant_id; not a tenant-owned table")
    return select(model).where(model.tenant_id == tenant_id)


def onto_key(tenant_id: uuid.UUID, *parts: str) -> str:
    """The one builder for tenant-prefixed cache/Redis keys: onto:{tenant_id}:..."""
    return ":".join(["onto", str(tenant_id), *parts])


def normalize_origin(url: str | None) -> str | None:
    """HTTPS://Odoo.Example.com:8069/web -> https://odoo.example.com:8069; None if not http(s)."""
    from urllib.parse import urlsplit

    try:
        p = urlsplit((url or "").strip())
        host = p.hostname
        port = p.port
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not host:
        return None
    return f"{p.scheme}://{host}" + (f":{port}" if port else "")


def tenant_by_url(session, url: str | None):
    """Registry lookup. None for an unregistered or malformed URL."""
    from app.db.onto import Tenant

    origin = normalize_origin(url)
    if origin is None:
        return None
    return session.exec(select(Tenant).where(Tenant.odoo_base_url == origin)).first()
