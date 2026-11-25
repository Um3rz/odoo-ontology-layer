# -*- coding: utf-8 -*-
"""Provisioning (1.5, contract 3.2a): create or update the managed datasets of one Onto release.

POST /onto/v1/provision (controllers/onto.py) calls provision(). Querygen is never called: each object
arrives as finished SQL, and this module checks it and builds its view. The rules, in order:

1. Shape: release.hash is a string; objects is a non-empty list; each object has the fields of 3.2a
   (label optional) and nothing else; key matches ^[a-z][a-z0-9_]{0,49}$ (fullmatch); keys are unique
   in the request; column names are identifiers and unique; spec_hash is 64 lowercase hex.
2. Groups: every group_xmlids entry resolves to a res.groups record. An empty list is refused unless
   engine_only.
3. Base model: base_model is a stored model of this database (ir.model, registry _auto).
4. SQL: one read-only SELECT (saas _validate_custom_sql and check_select), no runtime {{...}} tokens.
5. Relations: EXPLAIN (no ANALYZE, nothing runs) lists every relation the plan reads. Each must be a
   table of a stored model (public schema), and the base model's table must be one of them.
6. Columns: a LIMIT 0 probe in a read-only savepoint gives the output columns. _onto_id must be an
   Integer column (saas type mapping) and every declared column must be present.
7. Names: a dataset named onto_<key> or using view wad_onto_<key>_view that this key does not own is 409.

Rules 1-7 run for the whole request before anything is written (422 or 409). Then each object is built
on its own and committed, under its lock. Lock: each object holds pg_advisory_lock on a hash of
(database, key) from before its read until its commit or rollback, so a second run for the same key waits
and then sees the first run's build. The lock is session-level, not transaction-level, because the saas
generator commits in the middle of a build, and that would end a transaction lock.

An object whose SQL and base model are unchanged, whose view still has its columns and whose last build
finished keeps its view: "unchanged", or "updated" when only groups, engine_only, label or the hash changed.
A groups-only change updates group_ids. Anything else rebuilds the view once ("created" or "updated"). An
object that fails while building is rolled back to its last commit, reported as "failed", and the run goes on.

Accepted readings of 3.2a: a changed hash rebuilds only when the SQL, base model or view changed, because
groups and labels also change the hash. Column type, role, unit and PII belong to the querygen release, so
this module checks column names only. Query tokens get 403 on this route, not 404: onto_route refuses the
token kind before the steward check runs. Mobile and bot tokens of non-stewards get 404.

Lock on writes: the managed dataset and its lines refuse every write, create and unlink unless made inside
managed_dataset_unlocked(), which only this module enters. The flag is an in-process contextvar, so a client
context dict cannot set it.
"""

import hashlib
import logging
import re
from contextlib import contextmanager

import psycopg2

from odoo.exceptions import UserError

from odoo.addons.odoo_dashboards_saas.models.dataset import PG_TYPE_TO_FIELD_TYPE
from odoo.addons.odoo_dashboards_saas.models.sql_guard import check_select, read_only

from .onto_dataset import KEY_RE, UNLOCKED, dataset_name, slug, view_name

_logger = logging.getLogger(__name__)

OBJECT_FIELDS = frozenset(
    ["key", "label", "base_model", "sql", "columns", "group_xmlids", "engine_only", "spec_hash"]
)
REQUIRED_FIELDS = OBJECT_FIELDS - {"label"}
COLUMN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")
HASH_RE = re.compile(r"[0-9a-f]{64}")
META_FIELDS = ("description", "onto_engine_only", "onto_spec_hash", "onto_release")


class ProvisionError(Exception):
    """A refused request: status, code and message are what the route answers."""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _invalid(message):
    return ProvisionError(422, "invalid_spec", message)


def _first_line(exc):
    lines = str(exc).strip().splitlines()
    return lines[0] if lines else "database error"


@contextmanager
def managed_dataset_unlocked():
    """The only way past the managed-dataset lock. It sets an in-process flag that no client context reaches."""
    token = UNLOCKED.set(True)
    try:
        yield
    finally:
        UNLOCKED.reset(token)


def _lock_id(dbname, key):
    """A signed 64-bit advisory-lock id for one database and object key."""
    digest = hashlib.sha256(("%s:%s" % (dbname, key)).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _lock_object(env, key):
    """Waits until no other provision run holds this key, then drops the cache so this run reads the
    committed build. Session-level, because generate_dataset commits mid-build and would end a transaction lock."""
    # ponytail: no lock timeout; add lock_timeout if a hung run ever blocks the others.
    env.cr.execute("SELECT pg_advisory_lock(%s)", (_lock_id(env.cr.dbname, key),))
    env.invalidate_all()


def _unlock_object(env, key):
    env.cr.execute("SELECT pg_advisory_unlock(%s)", (_lock_id(env.cr.dbname, key),))


def _stored_models(env):
    """{model name: table} for every stored model of this database, as the registry loads it."""
    stored = {}
    for record in env["ir.model"].sudo().search([("transient", "=", False)]):
        cls = env.registry.get(record.model)
        if cls is not None and getattr(cls, "_auto", False) and getattr(cls, "_table", None):
            stored[record.model] = cls._table
    return stored


def _release_hash(release):
    if not isinstance(release, dict) or not isinstance(release.get("hash"), str):
        raise _invalid("release must be an object with a string hash")
    if not 0 < len(release["hash"]) <= 128:
        raise _invalid("release.hash must be 1 to 128 characters")
    return release["hash"]


def _group_for(env, xmlid):
    """The res.groups record of an external id, or None."""
    if "." not in xmlid:
        return None
    try:
        record = env.ref(xmlid, raise_if_not_found=False)
    except ValueError:
        return None
    return record if record and record._name == "res.groups" else None


def _parse_object(env, raw, stored):
    """One payload object, checked and normalised. Raises ProvisionError (422)."""
    if not isinstance(raw, dict) or not REQUIRED_FIELDS <= set(raw) or set(raw) - OBJECT_FIELDS:
        raise _invalid("each object needs the fields of contract 3.2a and no others")
    key = raw["key"]
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        raise _invalid("object key must match ^[a-z][a-z0-9_]{0,49}$")
    label, base_model, sql = raw.get("label", key), raw["base_model"], raw["sql"]
    if not isinstance(label, str) or not isinstance(base_model, str) or base_model not in stored:
        raise _invalid("object %s: base_model must be a stored model of this database" % key)
    if not isinstance(sql, str) or not sql.strip():
        raise _invalid("object %s: sql must be a non-empty string" % key)
    columns, xmlids, engine_only = raw["columns"], raw["group_xmlids"], raw["engine_only"]
    if not isinstance(columns, list) or not columns or not all(isinstance(c, dict) for c in columns):
        raise _invalid("object %s: columns must be a non-empty list of objects" % key)
    declared = [column.get("name") for column in columns]
    if any(not isinstance(name, str) or not COLUMN_RE.fullmatch(name) for name in declared) or len(
        set(declared)
    ) != len(declared):
        raise _invalid("object %s: column names must be unique identifiers" % key)
    if not isinstance(xmlids, list) or not all(isinstance(x, str) for x in xmlids) or not isinstance(
        engine_only, bool
    ):
        raise _invalid("object %s: group_xmlids must be strings and engine_only a boolean" % key)
    if not xmlids and not engine_only:
        raise _invalid("object %s: group_xmlids may be empty only for engine_only objects" % key)
    groups = env["res.groups"]
    for xmlid in xmlids:
        group = _group_for(env, xmlid)
        if group is None:
            raise _invalid("object %s: unknown group %s" % (key, xmlid))
        groups |= group
    if not isinstance(raw["spec_hash"], str) or not HASH_RE.fullmatch(raw["spec_hash"]):
        raise _invalid("object %s: spec_hash must be 64 lowercase hex characters" % key)
    return {
        "key": key, "label": label, "base_model": base_model, "sql": sql, "declared": declared,
        "group_ids": groups.ids, "engine_only": engine_only, "spec_hash": raw["spec_hash"],
    }


def _relations(node, found):
    """(schema, table) of every relation a plan node reads, at any depth."""
    if "Relation Name" in node:
        found.add((node.get("Schema"), node["Relation Name"]))
    for child in node.get("Plans", ()):
        _relations(child, found)
    return found


def _column_problem(pairs, declared):
    """Why a view's (column, saas type) pairs cannot serve the object, or None."""
    types = dict(pairs)
    if "_onto_id" not in types:
        return "the SQL must project the base id as _onto_id"
    if types["_onto_id"] != "Integer":
        return "_onto_id must be an integer column"
    missing = [name for name in declared if name not in types]
    return "the SQL does not project %s" % ", ".join(missing) if missing else None


def _probe(env, spec, stored):
    """Checks the SQL without creating anything. Raises ProvisionError (422)."""
    key = spec["key"]
    dataset = env["dataset.saas"].new({"sql": spec["sql"], "use_custom_query": True})
    try:
        if dataset._has_runtime_params():
            raise UserError("runtime parameters ({{...}}) are not allowed in managed SQL")
        dataset._validate_custom_sql()
        bare = dataset._bare_sql(check_select(spec["sql"]))
    except UserError as exc:
        raise _invalid("object %s: %s" % (key, exc.args[0]))
    try:
        with read_only(env.cr):
            # VERBOSE is what makes the plan name each relation's schema; without it every relation looks foreign.
            env.cr.execute("EXPLAIN (VERBOSE, FORMAT JSON) " + bare)
            plan = env.cr.fetchone()[0][0]["Plan"]
            env.cr.execute("SELECT * FROM (\n%s\n) AS onto_probe LIMIT 0" % bare)
            described = [(column[0], column[1]) for column in env.cr.description]
            pairs = []
            for name, oid in described:
                env.cr.execute("SELECT format_type(%s::oid, NULL)", (oid,))
                type_name = env.cr.fetchone()[0].split("(")[0].strip()
                pairs.append((name, PG_TYPE_TO_FIELD_TYPE.get(type_name, "Char")))
    except psycopg2.Error as exc:
        raise _invalid("object %s: the SQL does not run (%s)" % (key, _first_line(exc)))
    relations = _relations(plan, set())
    model_tables = set(stored.values())
    for schema, table in relations:
        if schema != "public" or table not in model_tables:
            raise _invalid("object %s: the SQL reads %s, which is not the table of a stored model" % (key, table))
    if ("public", stored[spec["base_model"]]) not in relations:
        raise _invalid("object %s: the SQL must read the base model's table" % key)
    problem = _column_problem(pairs, spec["declared"])
    if problem:
        raise _invalid("object %s: %s" % (key, problem))


def _clash(env, spec):
    """A dataset that holds this object's name or view but is not this object's managed dataset."""
    key, view = spec["key"], view_name(spec["key"])
    others = env["dataset.saas"].with_context(active_test=False).search(
        ["|", ("name", "=ilike", "onto%"), ("table", "=", view)]
    )
    for other in others:
        if other.onto_managed and other.onto_object_key == key:
            continue
        if slug(other.name) == "onto_" + key or other.table == view:
            return other
    return None


def _managed(env, key):
    return env["dataset.saas"].with_context(active_test=False).search(
        [("onto_managed", "=", True), ("onto_object_key", "=", key)], limit=1
    )


def _view_pairs(env, dataset):
    """(column, saas type) pairs of the dataset's view, or [] when the view does not exist."""
    if not dataset.table:
        return []
    env.cr.execute("SELECT to_regclass(%s) IS NOT NULL", (dataset.table,))
    return dataset._view_column_types() if env.cr.fetchone()[0] else []


def _same_build(env, dataset, spec):
    """True when the stored view is still built from this SQL and base model, has its columns, and its
    last build finished. A build that failed after its view was committed is left draft with no lines."""
    return (
        dataset.state == "active"
        and bool(dataset.line_ids)
        and dataset.sql == spec["sql"]
        and dataset.model_id.model == spec["base_model"]
        and _column_problem(_view_pairs(env, dataset), spec["declared"]) is None
    )


def _result(key, dataset_id, status):
    return {"key": key, "dataset_id": dataset_id, "view": view_name(key), "status": status, "error": None}


def _update_metadata(dataset, spec, values):
    """Same build: only metadata may change (groups, engine_only, label, hash, release stamp)."""
    groups_changed = set(dataset.group_ids.ids) != set(spec["group_ids"])
    hash_changed = dataset.onto_spec_hash != spec["spec_hash"]
    changes = {name: values[name] for name in META_FIELDS if dataset[name] != values[name]}
    if groups_changed:
        changes["group_ids"] = values["group_ids"]
    if changes:
        dataset.write(changes)
    status = "updated" if groups_changed or hash_changed else "unchanged"
    return _result(spec["key"], dataset.id, status)


def _provision_object(env, spec, release):
    """Builds one object's dataset and view. The caller commits. Raises on any failure."""
    key = spec["key"]
    model = env["ir.model"].sudo().search([("model", "=", spec["base_model"])], limit=1)
    values = {
        "name": dataset_name(key),
        "description": spec["label"],
        "sql": spec["sql"],
        "use_custom_query": True,
        "model_id": model.id,
        "group_ids": [(6, 0, spec["group_ids"])],
        "onto_managed": True,
        "onto_object_key": key,
        "onto_engine_only": spec["engine_only"],
        "onto_release": release,
        "onto_spec_hash": spec["spec_hash"],
    }
    existing = _managed(env, key)
    if existing and _same_build(env, existing, spec):
        return _update_metadata(existing, spec, values)
    if existing:
        status, dataset = "updated", existing
        dataset.write(dict(values, state="draft"))
    else:
        status = "created"
        dataset = env["dataset.saas"].create(dict(values, state="draft"))
    # The saas generator commits after it creates the view, so the draft state above is already safe.
    dataset.line_ids.unlink()
    dataset.generate_dataset()
    dataset.sync_lines_from_view_columns()
    problem = _column_problem(dataset._view_column_types(), spec["declared"])
    if problem:
        raise _invalid("object %s: %s" % (key, problem))
    dataset.write({"state": "active"})
    return _result(key, dataset.id, status)


def _error_text(exc):
    if isinstance(exc, ProvisionError):
        return exc.message
    if isinstance(exc, UserError) and exc.args:
        return str(exc.args[0])
    if isinstance(exc, psycopg2.Error):
        return _first_line(exc)
    return "could not be built; see the server log"


def _run(env, spec, release):
    """One object, committed on success. A failure rolls back to the last commit and is reported."""
    key = spec["key"]
    _lock_object(env, key)
    try:
        try:
            with managed_dataset_unlocked():
                result = _provision_object(env, spec, release)
            env.cr.commit()
            return result
        except Exception as exc:  # noqa: BLE001 - one object's failure must not stop the others
            env.cr.rollback()
            env.clear()
            _logger.warning("Onto provision: object %s not built (%s)", key, type(exc).__name__)
            dataset = _managed(env, key)
            return {
                "key": key, "dataset_id": dataset.id or None, "view": view_name(key),
                "status": "failed", "error": _error_text(exc),
            }
    finally:
        _unlock_object(env, key)


def provision(env, release, objects):
    """The 3.2a payload. Refuses the whole request (422 or 409) before writing; then builds each object."""
    release_hash = _release_hash(release)
    if not isinstance(objects, list) or not objects:
        raise _invalid("objects must be a non-empty list")
    stored = _stored_models(env)
    specs, keys = [], set()
    for raw in objects:
        spec = _parse_object(env, raw, stored)
        if spec["key"] in keys:
            raise _invalid("object %s is listed twice" % spec["key"])
        keys.add(spec["key"])
        _probe(env, spec, stored)
        specs.append(spec)
    for spec in specs:
        other = _clash(env, spec)
        if other:
            raise ProvisionError(
                409, "name_clash",
                "object %s: dataset '%s' already uses %s" % (spec["key"], other.name, view_name(spec["key"])),
            )
    return {"release": release_hash, "results": [_run(env, spec, release_hash) for spec in specs]}
