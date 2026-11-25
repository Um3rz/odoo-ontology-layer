"""The Builder (plan 2.3, D52): match the templates, draft the OrgMap, infer every candidate the schema offers (infer.py),
ask the model to curate them and to add what no object covers, and check every result against the steward's metadata
and the release compiler.

run() reads the steward's Odoo replies and returns the route's body and the proposal rows to store. It never calls
Odoo and never writes: the route stores the rows with store.replace_builder_proposals.
"""
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor

from app.ontology import loader
from app.ontology.builder import infer, llm, match
from app.ontology.builder.profile import InputError, Introspect, Profile
from app.ontology.schema import Link, Object, Release, Rule

logger = logging.getLogger(__name__)
BATCH_CONFIDENCE = 0.85  # plan 2.3 and 2.5: the least confidence batch approval takes (the route reads it)
__all__ = ["InputError", "approval_covers", "attach_proposal_ids", "run"]


def approval_covers(draft: dict, approved_role: str | None) -> bool:
    """2.9a: a location the steward approved with a role is not offered again. A draft that offers that role, or none
    (ambiguous: the steward already chose one), is covered; a draft that now offers another role is offered again."""
    return approved_role is not None and draft.get("proposed_role") in (None, approved_role)


def run(introspect: dict, profile: dict, domains: list[str] | None = None,
        approved: dict[int, str] | None = None, held: set[str] | None = None, *,
        ask_model=None) -> tuple[dict, list[dict]]:
    """`approved` maps a location id to the role the steward approved it with (store.approved_org_roles). `held` holds
    the release keys an approved proposal already has (store.approved_release_keys); the Builder does not offer them."""
    intro, prof = Introspect(introspect), Profile(profile)
    if not intro.odoo_version.startswith("16."):
        raise InputError(f"the Builder reads Odoo 16 only (D6), not {intro.odoo_version}")
    run_id = uuid.uuid4().hex
    release = loader.load_release()
    templates = loader._read_templates(loader.TEMPLATE_DIR, [])  # the release loaded above checked every file
    results = match.match_templates(templates, release, intro, prof)
    approved = approved or {}
    drafts = [d for d in match.orgmap_draft(intro) if not approval_covers(d, approved.get(d["location_id"]))]
    held = held or set()
    caveats = {path: note for template in templates for path, note in template.caveats.items()}
    # Infer runs with or without the LLM: the candidates are the schema's, the LLM only curates them.
    inferred, notes, candidate = infer.run(list(release.objects), release, intro, prof, run_id, held, caveats)
    llm_status, llm_rows, labels, drops = _llm(domains, candidate, intro, prof, run_id, results, _fields_with(intro),
                                               held, inferred, ask_model=ask_model)
    inferred, curated = infer.curate(inferred, labels, drops)
    notes += curated
    new = [row for row in llm_rows if row["kind"] == "object" and row["validation"]["ok"]]
    if new:  # a new object's properties go through the same pass
        objects = [Object.model_validate(row["payload"]["node"]) for row in new]
        links = [Link.model_validate(link) for row in new for link in row["payload"].get("links") or []]
        with_new = candidate.model_copy(update={"objects": [*candidate.objects, *objects],
                                                "links": [*candidate.links, *links]})
        more, more_notes, _ = infer.run(objects, with_new, intro, prof, run_id, held, caveats)
        inferred, notes = inferred + more, notes + more_notes
    rows = [*_template_rows(results, run_id), *_org_rows(drafts, run_id), *inferred, *llm_rows]
    body = {"run_id": run_id, "odoo_version": intro.odoo_version, "llm": llm_status,
            "template_proposals": results, "orgmap_draft": drafts, "inferred_proposals": inferred,
            "infer_notes": notes, "llm_proposals": llm_rows}
    return body, rows


def _fields_with(intro: Introspect) -> dict:
    """The compiler field list plus every stored field this database sent: its x_ fields and the fields of the
    non-standard modules the steward included. An object that binds one carries its type (match.own_fields)."""
    fields = {model: dict(entries) for model, entries in loader.load_compiler_fields().items()}  # cached: copy
    for model, meta in intro.models.items():
        known = fields.setdefault(model, {})
        for name, field in meta["fields"].items():
            if field.stored and name not in known:
                known[name] = {"type": field.type, "relation": field.relation, "store": True}
    return fields


def attach_proposal_ids(body: dict, rows: list[dict], ids: list[int]) -> None:
    """Name each stored row in the reply (Studio, plan 2.3): every LLM proposal and every template row gets its proposal_id.

    `ids` are the ids store.replace_builder_proposals returned, in the order of `rows`. An LLM proposal is the same dict
    as its row, so it carries proposal_id and confidence directly. A template result finds its row by node_key
    (template.<name>) and keeps the confidence it was stored with; a template that was not proposed has no row, so None.
    """
    for row, proposal_id in zip(rows, ids):
        row["proposal_id"] = proposal_id
    by_node = {row["node_key"]: row["proposal_id"] for row in rows}
    for result in body["template_proposals"]:
        result["proposal_id"] = by_node.get(f"template.{result['template']}")


def _domains(requested: list[str] | None, intro: Introspect) -> list[str]:
    """The domains to ask about: the steward's list, else the default plus every domain holding a custom field."""
    if requested is not None:
        unknown = sorted(set(requested) - set(llm.DOMAIN_PREFIXES))
        if unknown:
            raise InputError(f"unknown domains {unknown}; choose from {', '.join(llm.DOMAIN_PREFIXES)}")
        return list(dict.fromkeys(requested))
    custom = {llm.domain_of(model) for model, meta in intro.models.items()
              if any(f.custom for f in meta["fields"].values())}
    return sorted(set(llm.DEFAULT_DOMAINS) | (custom - {None}))


def _llm(requested, release: Release, intro: Introspect, prof: Profile, run_id: str, results: list[dict],
         fixture: dict, held: set[str], inferred: list[dict], *, ask_model=None) -> tuple[str, list[dict], list, list]:
    """The LLM's curation (plan 3): its checked proposals, and the labels and drops for the inferred rows. `release` is
    the candidate release: the templates plus the inferred rows, so a metric or rule may use a pending property."""
    wanted = _domains(requested, intro)
    if ask_model is None:
        if not llm.api_key_present():
            return f"skipped: no API key ({llm.key_env()})", [], [], []
        ask_model, prov = llm.ask, llm.provider()
    else:
        prov = "openai"
    # Every wanted domain is asked, with or without templates: coverage is the goal, and the steward reviews each
    # proposal. Objects in a domain without templates file under the domain keys core.yaml declares for it.
    keys = sorted(domain.key for domain in release.domains)
    covered = sorted(o.key for o in release.objects)
    taken_models = {o.base_model for o in release.objects}
    matched = match.matched_objects(results)
    measures = {o.key: [p.key for p in o.properties if p.role == "measure"] for o in release.objects}
    rules = {o.key: [r.key for r in release.rules if r.object == o.key] for o in release.objects}
    rows, failed, dropped, labels, drops = [], [], [], [], []
    payloads = {domain: llm.domain_input(domain, intro, prof, covered, matched, keys, fixture, measures=measures,
                                         rules=rules, objects=llm.object_views(domain, release, intro, prof, inferred),
                                         taken_models=taken_models) for domain in wanted}
    payloads = {domain: payload for domain, payload in payloads.items() if payload["models"] or payload["objects"]}

    def _ask(payload):
        try:
            return ask_model(payload)
        except Exception as exc:  # the API or its reply failed: this domain is left out and the others still run
            return exc

    # One call per domain, side by side: the slowest domain sets the wait, not their sum.
    with ThreadPoolExecutor(max_workers=max(1, min(4, len(payloads)))) as pool:
        replies = dict(zip(payloads, pool.map(_ask, payloads.values())))
    for domain in payloads:
        reply = replies[domain]
        if isinstance(reply, Exception):
            logger.warning("builder llm call failed for domain %s: %s", domain, type(reply).__name__)
            failed.append(domain)
            continue
        labels += reply.get("labels") or []
        drops += reply.get("drops") or []
        entries = reply["proposals"]
        # Rules first: a metric of the same reply may filter by one, so it is checked against the release with them.
        for batch in ([e for e in entries if isinstance(e, dict) and e.get("kind") == "rule"],
                      [e for e in entries if not (isinstance(e, dict) and e.get("kind") == "rule")]):
            settled, reasons = match.settle(batch, domain, release, fixture, intro, held)
            dropped += [reason for reason in reasons if reason]
            for entry, reason in zip(settled, reasons):
                if reason is not None:
                    continue
                row = match.llm_row(entry, domain, run_id, release, intro, prof)
                rows.append(row)
                if row["kind"] == "rule" and row["validation"]["ok"]:
                    rule = {k: row["payload"][k] for k in ("key", "label", "object", "when", "synonyms")}
                    release = release.model_copy(update={"rules": [*release.rules, Rule.model_validate(rule)]})
    if dropped:  # each reason names the key or model, never a record value
        logger.info("builder llm dropped %d proposal(s): %s", len(dropped), "; ".join(dropped))
    status = f"failed: {', '.join(failed)}" if failed else "ok"
    return f"{status} ({prov})", rows, labels, drops


def _template_rows(results: list[dict], run_id: str) -> list[dict]:
    rows = []
    for result in results:
        if not result["proposed"]:
            continue
        problems = [f"{o['object']}.{p['property']}: {p.get('problem', p['status'])}"
                    for o in result["objects"] for p in o["properties"] if p["status"] != "matched"]
        # D54: a template fit is informational, so it is never batch-approvable.
        rows.append({
            "node_key": f"template.{result['template']}",
            "payload": {"kind": "template", "source": "builder", "run_id": run_id, "template": result["template"],
                        "objects": result["objects"], "batch_approvable": False},
            "confidence": result["confidence"], "validation": {"ok": True, "problems": problems},
            "batch_approvable": False,
        })
    return rows


def _org_rows(drafts: list[dict], run_id: str) -> list[dict]:
    rows = []
    for draft in drafts:
        ok = not draft["ambiguous"]  # an ambiguous draft has no role to apply, so it fails validation
        batch = ok and draft["confidence"] >= BATCH_CONFIDENCE  # D54: batch lists it as a candidate, never approves it
        rows.append({
            "node_key": f"org_location.{draft['location_id']}",
            "payload": {"kind": "org_location", "source": "builder", "run_id": run_id, "batch_approvable": batch,
                        **draft},
            "confidence": draft["confidence"], "validation": {"ok": ok, "problems": [] if ok else [draft["problem"]]},
            "batch_approvable": batch,
        })
    return rows
