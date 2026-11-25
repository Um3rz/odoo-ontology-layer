# Usage: python scripts/onto_export_odoo16_fields.py <conf> <db> app/ontology/templates app/ontology/data/odoo16_fields.json
# <conf> is an Odoo-style conf file with db_host/db_port/db_user/db_password; the password is never in code or output.
# Run against a scratch Odoo 16 DB only (read-only queries). Re-run after any template binding change, then commit the JSON.
"""Export the Odoo 16 field fixture for exactly the paths the templates bind (plan 1.1).

Read-only: ir_model_fields of the throwaway DB onto_p11_fields. Every hop of every
binding path (including the leaf) is written as model -> field -> {type, relation, store}.
Usage: export_fixture.py <conf> <db> <templates_dir> <out.json>
The password is read from the conf file and never printed.
"""
import json
import re
import sys
from pathlib import Path

import psycopg2
import yaml

conf_path, db, templates_dir, out_path = sys.argv[1:5]
conf = open(conf_path, encoding="utf-8").read()
get = lambda k: re.search(r"^%s\s*=\s*(.*)$" % k, conf, re.M).group(1).strip()
cn = psycopg2.connect(host=get("db_host"), port=int(get("db_port")), user=get("db_user"),
                      password=get("db_password"), dbname=db)
cn.set_session(readonly=True, autocommit=True)
cur = cn.cursor()

# Collect (base_model, dotted path) for every Odoo 16 binding, including OrgRole joins.
paths = []
for f in sorted(Path(templates_dir).glob("*.yaml")):
    tpl = yaml.safe_load(f.read_text(encoding="utf-8"))
    objects = {o["key"]: o for o in tpl.get("objects", [])}
    for o in objects.values():
        for p in o.get("properties", []):
            if "16" in p.get("bindings", {}):
                paths.append((o["base_model"], p["bindings"]["16"], f"{f.name}:{o['key']}.{p['key']}"))
    for r in tpl.get("org_roles", []):
        base = objects.get(r["object"]) or {}
        if "16" in r.get("joins_on", {}):
            # Object of the role lives in this template, or in org.yaml for location.
            paths.append(("stock.location", r["joins_on"]["16"], f"{f.name}:org_role {r['key']}"))

fields_cache = {}


def model_fields(model):
    if model not in fields_cache:
        cur.execute("SELECT name, ttype, relation, store FROM ir_model_fields WHERE model = %s", (model,))
        fields_cache[model] = {n: {"type": t, "relation": rel, "store": bool(s)} for n, t, rel, s in cur.fetchall()}
    return fields_cache[model]


out = {}
problems = []
for base_model, path, where in paths:
    current = base_model
    segments = path.split(".")
    for i, seg in enumerate(segments):
        meta = model_fields(current).get(seg)
        if meta is None:
            problems.append(f"{where}: {current}.{seg} not in ir_model_fields")
            break
        out.setdefault(current, {})[seg] = meta
        if i < len(segments) - 1:
            if meta["type"] != "many2one":
                problems.append(f"{where}: {current}.{seg} is {meta['type']}, not many2one")
                break
            current = meta["relation"]

if problems:
    print("\n".join(problems))
    sys.exit(1)

out = {m: dict(sorted(f.items())) for m, f in sorted(out.items())}
Path(out_path).write_text(json.dumps(out, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
print("paths:", len(paths), "models:", len(out), "fields:", sum(len(f) for f in out.values()))
