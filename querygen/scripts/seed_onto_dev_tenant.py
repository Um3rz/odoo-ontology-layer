"""Register (or update) a tenant in the local registry. Dev helper (plan 0.6).

  python scripts/seed_onto_dev_tenant.py --url http://localhost:8070 \
      --db-uuid <database.uuid> --web-origin http://localhost:3000

Uses DATABASE_URL from .env. The database must already be migrated
(`alembic upgrade head`). Prints no credentials.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import Session  # noqa: E402

from app import db  # noqa: E402
from app.db.onto import Tenant  # noqa: E402
from app.db.tenant import normalize_origin, tenant_by_url  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--url", required=True, help="Odoo base URL (origin)")
ap.add_argument("--db-uuid", required=True)
ap.add_argument("--web-origin", action="append", default=[])
a = ap.parse_args()

url = normalize_origin(a.url)
if not url:
    sys.exit("invalid --url")
with Session(db.engine) as s:
    t = tenant_by_url(s, url) or Tenant(odoo_base_url=url, db_uuid=a.db_uuid)
    t.db_uuid = a.db_uuid
    t.web_origins = sorted({normalize_origin(o) for o in a.web_origin if normalize_origin(o)})
    s.add(t)
    s.commit()
    print(f"tenant {t.id} -> {t.odoo_base_url}")
