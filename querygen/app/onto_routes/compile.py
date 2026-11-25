"""POST /onto/compile: querygen's half of plan 1.6 (contract D48, D49). A pure function of (release, spec).

No customer data is read, no Odoo call is made and nothing is persisted. compile_query builds the
QueryRequests-shaped dict in memory; querygen's own SQL generators turn it into the final SELECT
(D49), called directly: no session, no save_to_db, no background task, so Odoo never needs
/query/querygen and no QueryRequests row is ever written.

Auth: the existing server-to-server key (X-API-Key, validated by app.utils.auth.get_api_key), with
X-Odoo-Url resolving the tenant through the registry. A refused key is 401 here (get_api_key says 403).
"""
import functools
import os
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, Security
from pydantic import ValidationError
from sqlmodel import Session

from app import db
from app.db.onto import Tenant
from app.db.tenant import tenant_by_url
from app.models.models import QueryRequests
from app.ontology import explore, store
from app.ontology.compiler import assert_visibility_predicate, compile_query, query_reads
from app.ontology.loader import load_release
from app.ontology.schema import Release
from app.ontology.spec import QuerySpec, SpecError
from app.utils import auth
from app.utils import browser_auth as ba
from app.utils import utils as query_utils
from app.utils.browser_auth import OntoAuthError

router = APIRouter()


@functools.lru_cache(maxsize=1)
def _templates() -> tuple[Release, str]:
    """The committed templates and their digest, loaded once. Used only when the hash matches them."""
    release = load_release()
    return release, release.digest()


def _templates_allowed() -> bool:
    """The committed templates may stand in for an unpublished hash only on a dev install (D49, item 4)."""
    return os.getenv("ENVIRONMENT") == "DEV" or os.getenv("ONTO_ALLOW_TEMPLATE_RELEASE") == "1"


async def server_key(request: Request, session: db.SessionType,
                     api_key: str | None = Security(auth.api_key_header)) -> str:
    try:
        return await auth.get_api_key(request=request, session=session, api_key_header=api_key)
    except HTTPException as exc:  # get_api_key answers 403 for a missing or unknown key
        raise OntoAuthError(401, "invalid_api_key") from exc


def registered_tenant(request: Request, session: db.SessionType, x_odoo_url: str | None = Header(default=None)) -> Tenant:
    tenant = tenant_by_url(session, x_odoo_url)
    if tenant is None:
        raise OntoAuthError(403, "unknown_tenant")
    principal = getattr(request.state, "principal", None)
    if principal and principal.kind == "install" and principal.install.uid != tenant.db_uuid:
        raise OntoAuthError(403, "unknown_tenant")
    if not ba.tenant_limiter.allow(str(tenant.id)):
        raise OntoAuthError(429, "rate_limited", {"Retry-After": "60"})
    return tenant


def _release(session: Session, tenant_id, digest: str) -> tuple[Release, str]:
    """The stored release for this tenant and hash; else the templates on a dev install when the hash is theirs."""
    try:
        return store.load_release(session, tenant_id, digest), "store"
    except LookupError:
        pass
    templates, templates_digest = _templates()
    if digest == templates_digest and _templates_allowed():
        return templates, "templates"
    raise HTTPException(404, "not found")


CURRENT = "current"  # a release name sent instead of a hash: this tenant's current release


def _current_hash(session: Session, tenant_id) -> str:
    """The hash of the release every read treats as current (store.current_release). 404 when there is none."""
    row = store.current_release(session, tenant_id)
    if row is None:
        raise HTTPException(404, "no_current_release")
    return row.hash


def _columns(release: Release, spec: QuerySpec, request: dict[str, Any]) -> list[dict]:
    """The logical result: each dimension, the time bucket when grained, then the queried metric (a
    derived metric's column is its key). Component measures are not columns."""
    obj = next(o for o in release.objects if o.key == spec.object)
    props = {p.key: p for p in obj.properties}
    metric = next(m for m in release.metrics if m.key == spec.metric)
    keys = list(spec.dims)
    if spec.grain:
        keys.append(request["matrix_col_dim"]["field_name"])
    columns = [{"key": k, "label": props[k].label, "role": props[k].role, "unit": props[k].unit} for k in keys]
    columns.append({"key": metric.key, "label": metric.label, "role": "measure", "unit": metric.unit})
    return columns


def _final_sql(request: dict[str, Any]) -> str:
    """querygen's own SQL generator for the compiled request, run in memory (D49). The request is
    validated as a QueryRequests, the body type of /query/querygen, and never added to a session."""
    try:
        nulls = {name: None for name, field in QueryRequests.model_fields.items() if field.is_required()}
        query_request = QueryRequests.model_validate(nulls | request)
        if query_request.analytic_type == "tile":
            sql = query_utils.generate_tile_query(query_request=query_request)
        else:
            sql = query_utils.generate_matrix_query(query_request=query_request)
        text = sql.strip()
        if text.endswith(";"):  # a statement terminator is not part of the SELECT Odoo wraps
            text = text[:-1].rstrip()
        return assert_visibility_predicate(text)
    except (ValueError, SpecError) as exc:  # querygen's validators, or a missing predicate: refused, not 500
        raise HTTPException(422, str(exc)) from exc


@router.post("/compile")
def compile_spec(
    body: dict,
    response: Response,
    session: db.SessionType,
    _key: str = Depends(server_key),
    tenant: Tenant = Depends(registered_tenant),
) -> dict:
    digest = body.get("release")
    if not isinstance(digest, str):
        raise HTTPException(422, "release is required")
    try:
        spec = QuerySpec.model_validate(body.get("spec"))
    except ValidationError as exc:
        error = exc.errors()[0]
        raise HTTPException(422, f"{'.'.join(map(str, error['loc']))}: {error['msg']}") from exc

    digest = _current_hash(session, tenant.id) if digest == CURRENT else digest
    if spec.release == CURRENT:
        spec = spec.model_copy(update={"release": digest})
    release, source = _release(session, tenant.id, digest)
    obj = next((o for o in release.objects if o.key == spec.object), None)
    metric = next((m for m in release.metrics if m.key == spec.metric and m.object == spec.object), None)
    if obj is None or metric is None:
        raise HTTPException(404, "not found")  # unknown and unpublished are not told apart

    try:
        compiled = compile_query(release, spec)
        reads = query_reads(release, spec)
    except SpecError as exc:
        raise HTTPException(422, str(exc)) from exc
    suspended = store.open_drift(session, tenant.id)  # plan 2.5: a property with an open drift row suspends what reads it
    for prop in sorted(reads["bindings"]):
        row = suspended.get(f"{spec.object}.{prop}")
        if row is not None:
            raise HTTPException(422, {"error": "suspended_by_drift", "binding": row.binding})

    sql = _final_sql(compiled["request"])
    previous_sql = _final_sql(compiled["previous_request"]) if compiled["previous_request"] else None
    response.headers["X-Onto-Release-Source"] = source
    return {
        "request": compiled["request"],
        "previous_request": compiled["previous_request"],  # null unless compare; merge is needed with it
        "merge": compiled["merge"],
        "derive": compiled["derive"],
        "columns": _columns(release, spec, compiled["request"]),
        "release": compiled["release"],
        "path": compiled["path"],  # "tile" or "matrix": the Odoo side checks it
        "sql": sql,  # querygen's final SELECT for `request` (D49)
        "previous_sql": previous_sql,  # querygen's SELECT for `previous_request`, or null
        "params": compiled["params"],  # name -> bound value; `sql` reads them as %(name)s, run it with this dict
        "bindings": reads["bindings"],  # prop key -> Odoo 16 field path the query reads
        "snapshot": reads["snapshot"],  # a balance read at the period end: the as_of rule applies
    }


@router.post("/explore/compile")
def compile_explore_route(
    body: dict,
    session: db.SessionType,
    _key: str = Depends(server_key),
    tenant: Tenant = Depends(registered_tenant),
) -> dict:
    """Odoo's /onto/v1/explore asks for the SQL of an uncertified exploration (plan 3.5). Stateless: no release."""
    try:
        return explore.compile_explore(body)
    except SpecError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/lookup/compile")
def compile_lookup_route(
    body: dict,
    session: db.SessionType,
    _key: str = Depends(server_key),
    tenant: Tenant = Depends(registered_tenant),
) -> dict:
    """Odoo's /onto/v1/lookup_values asks for the distinct-value SQL of one property (plan 3.5). PII is refused."""
    release_ref, object_key, column = body.get("release"), body.get("object"), body.get("column")
    if not isinstance(release_ref, str) or not isinstance(object_key, str) or not isinstance(column, str):
        raise HTTPException(422, "release, object and column are required")
    digest = _current_hash(session, tenant.id) if release_ref == CURRENT else release_ref
    release, _source = _release(session, tenant.id, digest)
    try:
        out = explore.compile_lookup(release, object_key, column)
    except LookupError as exc:  # unknown object or column (a one2many has no column either)
        raise HTTPException(404, "not found") from exc
    except SpecError as exc:  # PII, or the SQL failed querygen's checks
        raise HTTPException(422, str(exc)) from exc
    row = store.open_drift(session, tenant.id).get(f"{object_key}.{column}")  # plan 2.5: suspended bindings are not read
    if row is not None:
        raise HTTPException(422, {"error": "suspended_by_drift", "binding": row.binding})
    return {"release": digest, **out}
