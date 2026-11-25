"""Who is calling, which Odoo database it is, and whether it may continue.

Three kinds of key reach the API: a Polar license key (existing customers), an
embedded key shipped inside a module release, and an instance token minted
when a database activates its Odoo Apps purchase. The database identifies
itself on every request through the X-Odoo-* headers below.
"""
import math
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlmodel import Session, select

from app.models.models import EmbeddedKey, Install, License, utcnow_naive

H_UID = "x-odoo-instance"
H_URL = "x-odoo-url"
H_KIND = "x-odoo-kind"
H_MODULE = "x-module-version"
H_ODOO = "x-odoo-version"

EMBEDDED_PREFIX = "odb_"
TOKEN_PREFIX = "odi_"
GRACE_DAYS = int(os.getenv("LICENSE_GRACE_DAYS") or 14)
# What one store purchase buys: any two databases.
LICENSE_SEATS = int(os.getenv("LICENSE_SEATS") or 2)
TOUCH_EVERY = 600  # seconds between last_seen writes for one database

# A shipped key can only reach what the module itself calls. Restriction
# closes the work routes only: activation and credits must stay reachable.
WORK_PREFIXES = ("/query", "/dataset", "/visualization", "/dashboard", "/analyst")
OPEN_PREFIXES = ("/install", "/credits")
ALLOWED_PREFIXES = WORK_PREFIXES + OPEN_PREFIXES
ONTO_COMPILE_PATHS = {"/onto/compile", "/onto/explore/compile", "/onto/lookup/compile"}

RESTRICTED_MESSAGE = (
    "Your Odoo Dashboards license needs activation. "
    "Open Dashboards and click Activate License."
)


@dataclass
class Principal:
    kind: str  # embedded | install | license
    embedded: Optional[EmbeddedKey] = None
    install: Optional[Install] = None


@dataclass
class Identity:
    uid: str
    base_url: Optional[str] = None
    kind: Optional[str] = None
    module_version: Optional[str] = None
    odoo_version: Optional[str] = None


class SeatsFull(Exception):
    def __init__(self, urls: List[str]):
        super().__init__("seats full")
        self.urls = urls


def _naive(dt: Optional[datetime]) -> Optional[datetime]:
    # Stored and compared as naive UTC; an aware input is converted first.
    if dt is not None and dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    # Only for output: the stored naive UTC value, labelled.
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _now() -> datetime:
    return utcnow_naive()


def new_embedded_key() -> str:
    return EMBEDDED_PREFIX + secrets.token_hex(24)


def check_embedded_key(key: str) -> str:
    key = (key or "").strip()
    if not key.startswith(EMBEDDED_PREFIX) or len(key) < 32:
        raise ValueError('An embedded key starts with "odb_" and is at least 32 characters.')
    return key


def new_instance_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def identity_from_headers(headers) -> Optional[Identity]:
    uid = (headers.get(H_UID) or "").strip()
    if not uid:
        return None
    kind = (headers.get(H_KIND) or "").strip().lower() or None
    return Identity(
        uid=uid[:128],
        base_url=(headers.get(H_URL) or "").strip()[:512] or None,
        kind=kind if kind in ("production", "staging") else None,
        module_version=(headers.get(H_MODULE) or "").strip()[:32] or None,
        odoo_version=(headers.get(H_ODOO) or "").strip()[:32] or None,
    )


def resolve_principal(session: Session, key: str) -> Optional[Principal]:
    """None means "not ours": let the Polar license path decide."""
    if not key:
        return None
    if key.startswith(EMBEDDED_PREFIX):
        row = session.exec(select(EmbeddedKey).where(EmbeddedKey.key == key)).first()
        return Principal("embedded", embedded=row) if row else None
    if key.startswith(TOKEN_PREFIX):
        row = session.exec(select(Install).where(Install.token == key)).first()
        return Principal("install", install=row) if row else None
    return None


def _under(path: str, prefixes) -> bool:
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def path_allowed(path: str) -> bool:
    return _under(path, ALLOWED_PREFIXES)


def path_gated(path: str) -> bool:
    return path in ONTO_COMPILE_PATHS or _under(path, WORK_PREFIXES)


_touched: Dict[str, float] = {}


def touch_install(
    session: Session, identity: Optional[Identity], principal: Optional[Principal]
) -> Optional[Install]:
    """Upsert the calling database. Writes are throttled per uid because this
    runs on every request."""
    if principal is not None and principal.kind == "install":
        install = principal.install
        if identity is not None and identity.uid != install.uid:
            raise HTTPException(status_code=403, detail="This token belongs to another database.")
        if identity is None:
            return install
    if identity is None:
        return None

    install = session.exec(select(Install).where(Install.uid == identity.uid)).first()
    now = _now()
    if install is None:
        install = Install(
            uid=identity.uid,
            base_url=identity.base_url,
            kind=identity.kind,
            module_version=identity.module_version,
            odoo_version=identity.odoo_version,
            first_seen=now,
            last_seen=now,
        )
        session.add(install)
        session.commit()
        session.refresh(install)
        _touched[identity.uid] = time.monotonic()
        return install

    last = _touched.get(identity.uid)
    if last is not None and time.monotonic() - last < TOUCH_EVERY:
        return install
    install.last_seen = now
    install.base_url = identity.base_url or install.base_url
    install.module_version = identity.module_version or install.module_version
    install.odoo_version = identity.odoo_version or install.odoo_version
    # Activation fixes the kind; before that the database's own word stands.
    if install.kind is None:
        install.kind = identity.kind
    session.add(install)
    session.commit()
    _touched[identity.uid] = time.monotonic()
    return install


def install_access(
    install: Optional[Install], principal: Principal, now: Optional[datetime] = None
) -> Tuple[str, Optional[datetime]]:
    """('allowed' | 'grace' | 'restricted', grace_until). 'grace' still serves.
    May set install.grace_until; the caller commits."""
    now = _naive(now) or _now()
    if install is not None and install.status_override == "restricted":
        return "restricted", _naive(install.grace_until)
    if install is not None and install.status_override == "allowed":
        return "allowed", None
    if principal.kind == "license":
        return "allowed", None
    if install is not None and install.license_id:
        return "allowed", None
    if principal.kind == "embedded" and principal.embedded.revoked_at is None:
        return "allowed", None
    # Revoked generation (or a token whose license was removed) without a
    # license: the grace clock starts the first time we see the database.
    if install is None:
        return "restricted", None
    if install.grace_until is None:
        install.grace_until = now + timedelta(days=GRACE_DAYS)
    until = _naive(install.grace_until)
    return ("grace" if now < until else "restricted"), until


ACTIVATION_CLOSED_MESSAGE = "License activation is not open yet."


def activation_allowed(principal: Principal, access: str, now: Optional[datetime] = None) -> bool:
    """Open once it is needed (grace or restricted) or once ops opened it for
    the generation the database runs on."""
    if access in ("grace", "restricted"):
        return True
    if principal.kind == "embedded" and principal.embedded is not None:
        opens = _naive(principal.embedded.activation_opens_at)
        return opens is not None and (_naive(now) or _now()) >= opens
    return False


def status_payload(
    install: Optional[Install],
    principal: Principal,
    access: str,
    grace_until: Optional[datetime],
    now: Optional[datetime] = None,
) -> dict:
    now = _naive(now) or _now()
    grace_until = _naive(grace_until)
    licensed = principal.kind == "license" or bool(install and install.license_id)
    if principal.kind == "license":
        ai_bound = True
    else:
        ai_bound = bool(install and install.polar_customer_id)

    if access == "restricted":
        status = "restricted"
    elif access == "grace":
        status = "grace"
    elif licensed:
        status = "licensed"
    else:
        status = "early_access"

    days_left = None
    if grace_until is not None and status == "grace":
        # Rounded up: a grace that just started reads "14 days", not 13.
        days_left = max(0, math.ceil((grace_until - now).total_seconds() / 86400))

    activation_open = not licensed and activation_allowed(principal, access, now)
    notice = None
    if status == "early_access" and activation_open:
        notice = {
            "level": "info",
            "message": "Your Odoo Dashboards license can now be activated.",
            "action": {"kind": "activate", "label": "Activate License"},
        }
    elif status == "grace":
        notice = {
            "level": "warning",
            "message": f"Your Odoo Dashboards license needs activation. {days_left} days left.",
            "action": {"kind": "activate", "label": "Activate License"},
        }
    elif status == "restricted":
        notice = {
            "level": "danger",
            "message": RESTRICTED_MESSAGE,
            "action": {"kind": "activate", "label": "Activate License"},
        }
    return {
        "status": status,
        "licensed": licensed,
        "kind": install.kind if install else None,
        "ai_bound": ai_bound,
        "grace_until": _aware(grace_until).isoformat() if grace_until else None,
        "days_left": days_left,
        "activation_open": activation_open,
        "notice": notice,
    }


def seat_kind(install: Install, identity: Optional[Identity] = None) -> str:
    return install.kind or (identity.kind if identity else None) or "production"


def bind_install_to_license(
    session: Session, install: Install, licenses: List[License], kind: str
) -> License:
    """First license of this email with a free seat, else SeatsFull naming the
    databases that hold the seats. `kind` is recorded, not counted: staging
    and production are not told apart reliably enough to sell separately."""
    taken: List[str] = []
    for lic in licenses:
        bound = session.exec(
            select(Install).where(Install.license_id == lic.id, Install.id != install.id)
        ).all()
        if len(bound) < lic.seats:
            install.license_id = lic.id
            install.kind = kind
            return lic
        taken.extend(b.base_url or b.uid for b in bound)
    # The same database can sit on several of the email's licenses; name it once.
    raise SeatsFull(list(dict.fromkeys(taken)))
