# Usage: python scripts/onto_export_odoo16_catalog.py <conf> <db> <odoo_addons_dir> app/ontology/data/odoo16_catalog.json
# <conf> is an Odoo-style conf file with db_host/db_port/db_user/db_password; the password is never in code or output.
# Read-only. Run against an Odoo 16 DB with the business apps installed; re-run after installing more apps.
"""Export every stored business field that standard Odoo 16 modules define, for the Builder and compiler.

odoo16_fields.json stays exactly the template bindings (test_templates). This catalogue is the wider set the
Builder may propose bindings on: models under the business roots, stored fields of a type the compiler reads,
defined by a module shipped in <odoo_addons_dir> or base. Custom modules and x_ fields are left out.
"""
import json
import re
import sys
from pathlib import Path

import psycopg2

ROOTS = ("account", "calendar", "crm", "fleet", "hr", "helpdesk", "maintenance", "mrp", "pos", "product",
         "project", "purchase", "quality", "repair", "sale", "stock", "uom")
RES_MODELS = ("res.partner", "res.company", "res.country", "res.currency")
TYPES = ("float", "monetary", "integer", "boolean", "date", "datetime", "char", "selection", "text", "html", "many2one")

conf_path, db, addons_dir, out_path = sys.argv[1:5]
conf = open(conf_path, encoding="utf-8").read()
get = lambda k: re.search(r"^%s\s*=\s*(.*)$" % k, conf, re.M).group(1).strip()
standard = sorted({p.name for p in Path(addons_dir).iterdir() if (p / "__manifest__.py").exists()} | {"base"})

cn = psycopg2.connect(host=get("db_host"), port=int(get("db_port")), user=get("db_user"),
                      password=get("db_password"), dbname=db)
cn.set_session(readonly=True, autocommit=True)
cur = cn.cursor()
cur.execute("""
    SELECT f.model, f.name, f.ttype, f.relation
      FROM ir_model_fields f
      JOIN ir_model m ON m.id = f.model_id AND NOT m.transient
     WHERE f.store AND f.ttype = ANY(%s) AND f.name NOT LIKE 'x\\_%%'
       AND (split_part(f.model, '.', 1) = ANY(%s) OR f.model = ANY(%s))
       AND to_regclass(replace(f.model, '.', '_')) IS NOT NULL
       AND EXISTS (SELECT 1 FROM ir_model_data d WHERE d.model = 'ir.model.fields' AND d.res_id = f.id
                   AND d.module = ANY(%s))
""", (list(TYPES), list(ROOTS), list(RES_MODELS), standard))
out = {}
for model, name, ttype, relation in cur.fetchall():
    out.setdefault(model, {})[name] = {"relation": relation, "store": True, "type": ttype}
out = {m: dict(sorted(f.items())) for m, f in sorted(out.items())}
Path(out_path).write_text(json.dumps(out, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
print("models:", len(out), "fields:", sum(len(f) for f in out.values()))
