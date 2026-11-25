"""The provision payload (plan 2.5, contract 8, Part D 3.2a): every object of a release as the SQL Odoo installs for it.

Built with compiler.compile_object, so one release always gives the same spec_hash (sha256 of key, base_model, sql,
columns, group_xmlids and engine_only). The steward's browser posts the payload to Odoo /onto/v1/provision (D52);
querygen holds no Odoo credential and makes no call here.
"""
from app.ontology import compiler
from app.ontology.schema import Release


def engine_only(group_xmlids, stored: bool = False) -> bool:
    """An object with no access groups is engine only: Odoo refuses an empty group_xmlids unless engine_only (3.2a)."""
    return bool(stored or not group_xmlids)


def payload(release: Release, provenance: dict[str, dict], digest: str) -> dict:
    """`provenance` maps an object key to its stored access facts (group_xmlids, engine_only). A missing entry means no access."""
    objects = []
    for obj in release.objects:
        access = provenance.get(obj.key) or {}
        groups = access.get("group_xmlids", [])
        objects.append(compiler.compile_object(
            release, obj.key, release.odoo_version,
            group_xmlids=groups, engine_only=engine_only(groups, access.get("engine_only", False))))
    return {"release": {"hash": digest}, "objects": objects}
