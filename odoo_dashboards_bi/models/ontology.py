import json

import psycopg2

from odoo import _, api, fields, models, release
from odoo.exceptions import AccessError, UserError

from odoo.addons.odoo_dashboards_bi.models import onto_provision
from odoo.addons.odoo_dashboards_bi.models.onto_query import QuerygenError, querygen_call

from . import evidence, onto_query


DEFAULT_MODELS = (
    "sale.order", "sale.order.line", "purchase.order", "purchase.order.line", "product.product",
    "product.template", "product.category", "res.partner", "res.company", "res.currency",
    "stock.quant", "stock.move", "stock.move.line", "stock.picking", "stock.picking.type",
    "stock.location", "stock.warehouse", "stock.valuation.layer", "stock.lot", "uom.uom",
    "account.move", "account.move.line", "account.payment", "account.partial.reconcile", "crm.lead", "crm.team",
    "pos.order", "pos.order.line", "pos.session", "pos.config", "product.supplierinfo",
    "stock.scrap",
)
PREFIX = "native-bi/v1/ontology"
MODULES_PARAM = "odoo_dashboards_bi.ontology_modules"  # JSON list of the modules mapped; unset = the standard apps
OWN_MODULES = ("odoo_dashboards", "od_")  # this product's own modules are never mapped
MAX_SOURCE_MODELS = 300  # querygen asks one domain per call and keeps 40 models of each


class NativeBiOntology(models.AbstractModel):
    _name = "native.bi.ontology"
    _description = "BI ontology settings"

    @api.model
    def _check_admin(self):
        """The ontology screen (setup, build, review, map) is for administrators only: view and use."""
        if not self.env.user.active or not self.env.user.has_group("base.group_system"):
            raise AccessError(_("Only an administrator can manage the ontology."))

    @api.model
    def _request(self, path, payload, timeout=30):
        try:
            return querygen_call(self.env, PREFIX + path, payload, timeout=timeout)
        except QuerygenError as exc:
            if exc.status in (401, 403):
                raise UserError(_("Activate this Odoo installation and register it for native BI in Querygen.")) from exc
            if exc.status == 409:
                raise UserError(_("The proposal or release changed. Refresh the ontology and try again.")) from exc
            if exc.status == 422 and exc.message:
                raise UserError(exc.message) from exc
            raise UserError(_("The ontology service is unavailable. Please try again.")) from exc

    @api.model
    def get_status(self):
        self._check_admin()
        switch = self.env["onto.agent.setting"].onto_switches({})
        config = self.env["ir.config_parameter"].sudo()
        installed_release = config.get_param("odoo_dashboards_bi.release") or None
        settings = {
            "enabled": switch["ai_enabled"], "paused": switch["agents_paused"],
            "installed_release": installed_release,
            "pending_release": config.get_param("odoo_dashboards_bi.pending_release") or None,
            "previous_release": config.get_param("odoo_dashboards_bi.previous_release") or None,
            "source_models": self._source_models(),
            "module_choices": self._module_choices(),
        }
        if not settings["enabled"]:
            return {**settings, "metrics": [], "objects": [], "proposals": []}
        # A missing activation or unreachable server should not hide the local opt-in.
        try:
            response = self._request("/status", {})
        except UserError as exc:
            return {**settings, "metrics": [], "objects": [], "proposals": [], "error": str(exc)}
        descriptions = self._object_descriptions()
        for key in ("objects", "draft_objects"):
            for obj in response.get(key, []):
                entry = descriptions.get(obj["key"], {})
                obj.update(description=entry.get("text"), description_source=entry.get("source"))
        reconciled = evidence.reconciled_metrics(self.env, installed_release)
        if response.get("release") == installed_release:
            for metric in response.get("metrics", []):
                if metric["key"] in reconciled:
                    metric.update(trust="certified", reconciliation=reconciled[metric["key"]])
        object_keys = {obj["key"] for obj in response.get("objects", [])}
        if {"billing_line", "sales_order_line"} <= object_keys:
            try:
                line = evidence.model_for(self.env, "account.move.line")
                evidence.model_for(self.env, "sale.order.line")
            except AccessError:
                pass
            else:
                if "sale_line_ids" in line.fields_get(["sale_line_ids"]):
                    response["navigation_links"] = [{
                        "key": "invoice_bills_sales_lines", "source": "billing_line", "target": "sales_order_line",
                        "label": _("Bills sales order lines"), "cardinality": "m:m", "navigation_only": True,
                    }]
        # A review that confirms an auto-approved proposal keeps the release hash, yet the answers' "not yet reviewed"
        # line changes: publishing again refreshes the installed catalogue.
        catalog = json.loads(config.get_param("odoo_dashboards_bi.catalog") or "{}")
        live = sorted(m["key"] for m in catalog.get("metrics", []) if m.get("unreviewed"))
        if installed_release and "unreviewed_metrics" in response and response["unreviewed_metrics"] != live:
            response["publishable"] = True
        return {**response, **settings}

    @api.model
    def reconcile_metrics(self):
        """Independently compare baseline counts and currency-separated sales with native Odoo."""
        self._check_admin()
        evidence.check_enabled(self.env, self.env.companies.ids, check_credits=False)
        config = self.env["ir.config_parameter"].sudo()
        digest = config.get_param("odoo_dashboards_bi.release")
        if not digest or config.get_param("odoo_dashboards_bi.pending_release"):
            raise UserError(_("Install an active ontology release before reconciling metrics."))
        self.env.cr.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (evidence.RECONCILIATION,))
        previous = json.loads(config.get_param(evidence.RECONCILIATION) or "{}")
        companies = previous.get("companies", {}) if previous.get("release") == digest else {}
        for company in self.env.companies:
            env = api.Environment(self.env.cr, self.env.uid, dict(self.env.context, allowed_company_ids=[company.id]))
            catalogue = {m["key"]: m for m in evidence.catalog(env, {})["metrics"]}
            company_domain = [("company_id", "=", company.id)]
            orders = [("state", "in", ["sale", "done"])]
            date = fields.Datetime.to_string(fields.Datetime.now())
            specs = {
                "order_count": ("sale.order", company_domain + orders, None),
                "pos_order_count": ("pos.order", company_domain + [("state", "in", ["paid", "done", "invoiced"])], None),
                "net_revenue": ("sale.order.line", company_domain + [("order_id.state", "in", ["sale", "done"])],
                                "price_subtotal"),
            }
            checks = {}
            for key, (model, domain, measure) in specs.items():
                metric = catalogue.get(key)
                if not metric:
                    continue
                records = env[model]
                if measure:
                    currencies = records.read_group(domain, ["currency_id"], ["currency_id"], lazy=False)
                    if len(currencies) != 1:
                        continue
                    expected = records.read_group(domain, [measure + ":sum"], [])[0][measure] or 0
                else:
                    expected = records.search_count(domain)
                try:
                    result = onto_query.run_query(env, {"release": digest, "object": metric["object"], "metric": key},
                                                  [company.id], scope_related=True)
                except onto_query.QueryError:
                    continue
                if len(result["rows"]) == 1:
                    actual = result["rows"][0][-1] or 0
                    checks[key] = {
                        "passed": abs(float(actual) - float(expected)) < 0.01,
                        "source": model, "checked_at": date,
                    }
            companies[str(company.id)] = checks
        config.set_param(evidence.RECONCILIATION, json.dumps({"release": digest, "companies": companies}))
        return {"checks": companies}

    @api.model
    def _object_descriptions(self):
        config = self.env["ir.config_parameter"].sudo()
        return json.loads(config.get_param(evidence.OBJECT_DESCRIPTIONS) or "{}")

    @api.model
    def dashboard_lineage(self):
        self._check_admin()
        dashboards = self.env["dashboards.saas"]
        datasets = self.env["dataset.saas"]
        if not dashboards.check_access_rights("read", raise_exception=False):
            return []
        groups = self.env.user.groups_id
        result = []
        for dashboard in dashboards.search([], order="name, id")._external_visible():
            widgets = dashboard._visible_analytics(dashboard)
            if not widgets.check_access_rights("read", raise_exception=False):
                continue
            widgets = widgets.search([("id", "in", widgets.ids)], order="sequence, id")
            readable = datasets.search([("id", "in", widgets.mapped("dataset_id").ids)]) if (
                datasets.check_access_rights("read", raise_exception=False)
            ) else datasets
            readable = readable.filtered(lambda data: not data.onto_engine_only and
                                         (not data.group_ids or bool(data.group_ids & groups)))
            rows = []
            for widget in widgets:
                dataset = widget.dataset_id & readable
                rows.append({
                    "id": widget.id, "label": widget.name,
                    "dataset": {
                        "id": dataset.id, "label": dataset.name,
                        "object": dataset.onto_object_key if dataset.onto_managed else None,
                        "model": dataset.model_id.model,
                    } if dataset else None,
                })
            if rows:
                result.append({"id": dashboard.id, "label": dashboard.name, "widgets": rows})
        return result

    @api.model
    def _write_descriptions(self, updates, *, only_missing=False):
        self.env.cr.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (evidence.OBJECT_DESCRIPTIONS,))
        config = self.env["ir.config_parameter"].sudo()
        parameter = config.search([("key", "=", evidence.OBJECT_DESCRIPTIONS)], limit=1)
        existing = json.loads(parameter.value or "{}")
        if only_missing:
            updates = {key: entry for key, entry in updates.items() if key not in existing}
        if updates:
            config.set_param(evidence.OBJECT_DESCRIPTIONS, json.dumps({**existing, **updates}))
        return len(updates)

    @api.model
    def generate_descriptions(self):
        self._check_admin()
        evidence.check_enabled(self.env, self.env.companies.ids)
        status = self._request("/status", {})
        existing = self._object_descriptions()
        keys = list(dict.fromkeys(
            obj["key"] for obj in status.get("objects", []) + status.get("draft_objects", [])
            if obj["key"] not in existing
        ))[:100]
        if not keys:
            return {"saved": 0}
        result = self._request("/descriptions", {"keys": keys}, timeout=75).get("descriptions") or {}
        updates = {key: {"text": text.strip(), "source": "ai"} for key, text in result.items()
                   if key in keys and isinstance(text, str) and 0 < len(text.strip()) <= 400}
        return {"saved": self._write_descriptions(updates, only_missing=True)}

    @api.model
    def save_description(self, key, text):
        self._check_admin()
        evidence.check_enabled(self.env, self.env.companies.ids, check_credits=False)
        if not isinstance(key, str) or not isinstance(text, str) or len(text.strip()) > 400:
            raise UserError(_("Enter an object description of up to 400 characters."))
        status = self._request("/status", {})
        if not any(obj["key"] == key for obj in status.get("objects", []) + status.get("draft_objects", [])):
            raise UserError(_("This object is no longer in the ontology. Refresh and try again."))
        self._write_descriptions({key: {"text": text.strip(), "source": "owner", "uid": self.env.uid}})
        return True

    @api.model
    def set_enabled(self, enabled):
        self._check_admin()
        if type(enabled) is not bool:
            raise UserError(_("Use the BI settings switch."))
        return self.env["onto.agent.setting"].onto_switches({"ai_enabled": enabled})

    @api.model
    def _model_modules(self):
        """{model: the module that defines it} for every non-transient model."""
        self.env.cr.execute("""
            SELECT DISTINCT ON (d.res_id) m.model, d.module
              FROM ir_model_data d JOIN ir_model m ON m.id = d.res_id
             WHERE d.model = 'ir.model' AND NOT m.transient
             ORDER BY d.res_id, d.id
        """)
        return dict(self.env.cr.fetchall())

    @api.model
    def _module_choices(self):
        """The installed modules whose models the ontology can map. A standard (Odoo S.A.) module is listed when one
        of its models is a business model; any other module when one of its models holds records."""
        origin = self._model_modules()
        modules = self.env["ir.module.module"].sudo().search([
            ("state", "=", "installed"), ("name", "in", list(set(origin.values()))),
        ])
        standard = {m.name for m in modules if m.author == "Odoo S.A."}
        counts = {}
        for model, module in origin.items():
            if module.startswith(OWN_MODULES) or model not in self.env:
                continue
            if module in standard:
                try:
                    evidence.model_for(self.env, model)
                except AccessError:
                    continue
            elif not self._has_records(model):
                continue
            counts[module] = counts.get(module, 0) + 1
        chosen = self._chosen_modules(standard)
        return sorted(({"module": m.name, "label": m.shortdesc or m.name, "standard": m.name in standard,
                        "models": counts[m.name], "selected": m.name in chosen}
                       for m in modules if m.name in counts), key=lambda row: (not row["standard"], row["label"]))

    @api.model
    def _has_records(self, model):
        try:
            target = self.env[model]
            if target._abstract:
                return False
            # to_regclass gives NULL for a missing table or view, so no failed query is logged for it.
            self.env.cr.execute("SELECT to_regclass(%s)", (target._table,))
            if self.env.cr.fetchone()[0] is None:
                return False
            with self.env.cr.savepoint():
                return bool(target.sudo().search([], limit=1))
        except Exception:
            return False

    @api.model
    def _chosen_modules(self, standard=None):
        saved = self.env["ir.config_parameter"].sudo().get_param(MODULES_PARAM)
        if saved:
            return set(json.loads(saved))
        if standard is None:
            standard = set(self.env["ir.module.module"].sudo().search([
                ("state", "=", "installed"), ("author", "=", "Odoo S.A."),
            ]).mapped("name"))
        return set(standard)

    @api.model
    def set_ontology_modules(self, modules):
        """The steward's pick of the modules to map. A non-standard module's models also become readable by BI."""
        self._check_admin()
        choices = {row["module"]: row for row in self._module_choices()}
        if not isinstance(modules, list) or not set(modules) <= set(choices):
            raise UserError(_("Choose modules from the list."))
        config = self.env["ir.config_parameter"].sudo()
        config.set_param(MODULES_PARAM, json.dumps(sorted(modules)))
        origin = self._model_modules()
        extra = sorted(model for model, module in origin.items()
                       if module in modules and not choices[module]["standard"] and model in self.env)
        config.set_param(evidence.EXTRA_MODELS_PARAM, json.dumps(extra))
        return True

    @api.model
    def approve_all(self):
        """Approve every pending proposal that would publish. Querygen lists any it left, with the reason."""
        self._check_admin()
        evidence.check_enabled(self.env, self.env.companies.ids)
        return self._request("/approve-all", {"uid": self.env.uid})

    @api.model
    def _source_models(self):
        """Every readable business model with records, from the modules the steward chose. DEFAULT_MODELS lead."""
        installed = self.env["ir.model"].sudo().search([("transient", "=", False)], order="model").mapped("model")
        origin, chosen = self._model_modules(), self._chosen_modules()
        result = []
        for name in dict.fromkeys((*DEFAULT_MODELS, *installed)):
            if origin.get(name) not in chosen:
                continue
            try:
                model = evidence.model_for(self.env, name)
                with self.env.cr.savepoint():  # a model without a table fails here, not the whole request
                    if not model.search([], limit=1):
                        continue
            except (AccessError, psycopg2.Error):
                continue
            result.append({"model": name, "label": model._description})
        return result

    @api.model
    def _builder_inputs(self, model_names):
        models_meta, profiles = [], []
        for name in dict.fromkeys(model_names):
            try:
                model = evidence.model_for(self.env, name)
            except AccessError:
                continue
            # The caller sees only permitted field metadata and aggregate counts. Reuse the Builder's input shape.
            info = model.fields_get(attributes=["string", "type", "relation", "selection", "store"])
            allowed = evidence.field_map(model)
            fields_meta = [
                {"name": key, **{field: val for field, val in desc.items() if field in {
                    "string", "type", "relation", "selection", "store",
                }}, "custom": key.startswith("x_")}
                for key, desc in info.items() if key in allowed
            ]
            models_meta.append({"model": name, "name": model._description, "fields": fields_meta})
            profile_fields = []
            with self.env.cr.savepoint():
                self.env.cr.execute("SET LOCAL statement_timeout = '5000ms'")
                count = model.search_count([])
                if count:
                    # One grouped count per model: COUNT(field) is the non-null rows of every field at once.
                    try:
                        with self.env.cr.savepoint():
                            totals = model.read_group([], ["%s:count" % key for key in allowed], [], lazy=False)[0]
                    except (psycopg2.Error, ValueError, UserError):
                        totals = {}
                    for key in allowed:
                        if key in totals:
                            profile_fields.append({"name": key, "fill_rate": min(1, (totals[key] or 0) / count)})
                self.env.cr.execute("SET LOCAL statement_timeout = DEFAULT")
            profiles.append({"model": name, "row_count": count, "fields": profile_fields})
        return {
            "introspection": {"odoo_version": release.series, "models": models_meta},
            "profile": {"models": profiles, "facts": {}},
        }

    @api.model
    def build_draft(self, models=None):
        self._check_admin()
        evidence.check_enabled(self.env, self.env.companies.ids)
        if models:
            if not isinstance(models, list) or len(models) > MAX_SOURCE_MODELS or not all(isinstance(name, str) for name in models):
                raise UserError(_("Choose up to %s Odoo models to map.", MAX_SOURCE_MODELS))
        selected = models or [item["model"] for item in self._source_models()]
        if not selected:
            raise UserError(_("No readable business models are installed."))
        result = self._request("/draft", self._builder_inputs(selected[:MAX_SOURCE_MODELS]), timeout=900)  # terra builds run past 4 min
        return self._build_and_go(result)

    @api.model
    def _build_and_go(self, result):
        """After a draft: apply the unambiguous location roles, let the Builder approve what passed validation, then
        publish. Review stays optional. A failure here leaves the approvals in place and is reported in the result."""
        drafts = [p for p in result.get("proposals") or [] if p.get("kind") == "org_location"
                  and p.get("state") == "proposed" and (p.get("validation") or {}).get("ok")
                  and not (p.get("payload") or {}).get("ambiguous") and (p.get("payload") or {}).get("proposed_role")]
        applied = []
        if drafts:
            entries = [{"location_id": p["payload"]["location_id"], "role": p["payload"]["proposed_role"]} for p in drafts]
            outcomes = self.env["onto.org.map"].apply_location_roles(entries)
            applied = [p["id"] for p, out in zip(drafts, outcomes) if out["status"] != "failed"]
        try:
            result["auto"] = self._request("/auto-approve", {"uid": self.env.uid, "org_locations": applied}, timeout=120)
            result["publish"] = self.publish()
        except UserError as exc:
            result["publish"] = {"status": "failed", "error": str(exc)}
        return result

    @api.model
    def review(self, proposal_id, decision, role=None):
        self._check_admin()
        evidence.check_enabled(self.env, self.env.companies.ids)
        if type(proposal_id) is not int or proposal_id <= 0 or decision not in ("approved", "revoked"):
            raise UserError(_("Choose a draft proposal and a review decision."))
        payload = {"proposal_id": proposal_id, "state": decision, "uid": self.env.uid}
        if role:
            payload["role"] = role
        return self._request("/review", payload)

    @api.model
    def publish(self, release=None):
        self._check_admin()
        evidence.check_enabled(self.env, self.env.companies.ids)
        self.env.cr.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("native_bi_activation",))
        config = self.env["ir.config_parameter"].sudo()
        previous = config.get_param("odoo_dashboards_bi.previous_release")
        if release and release == previous:
            candidate = json.loads(config.get_param("odoo_dashboards_bi.previous_candidate") or "{}")
        else:
            candidate = self._request("/publish", {"uid": self.env.uid}, timeout=60)
        digest = candidate.get("release")
        if release and release != digest:
            raise UserError(_("The candidate changed. Refresh and publish the current release."))
        provision = candidate.get("provision") or {}
        release_meta = provision.get("release") or {}
        if not digest or release_meta.get("hash") != digest:
            raise UserError(_("Querygen did not return a complete ontology candidate."))
        try:
            # The shared provisioner commits each view build. A surrounding savepoint cannot survive those commits.
            result = onto_provision.provision(self.env, release_meta, provision.get("objects"))
        except onto_provision.ProvisionError as exc:
            raise UserError(exc.message) from exc
        for item in result["results"]:
            if item["status"] not in ("created", "updated", "unchanged"):
                raise UserError(_("Could not provision %(object)s: %(error)s") % {
                    "object": item["key"], "error": item.get("error") or _("Unknown error"),
                })
        config.set_param("odoo_dashboards_bi.pending_release", digest)
        try:
            self._request("/activate", {"release": digest, "uid": self.env.uid}, timeout=60)
        except UserError as exc:
            return {"release": digest, "status": "pending", "error": str(exc)}
        current = config.get_param("odoo_dashboards_bi.release")
        if current and current != digest:
            config.set_param("odoo_dashboards_bi.previous_release", current)
            config.set_param("odoo_dashboards_bi.previous_candidate",
                             config.get_param("odoo_dashboards_bi.active_candidate"))
        config.set_param("odoo_dashboards_bi.release", digest)
        config.set_param("odoo_dashboards_bi.active_candidate", json.dumps(candidate))
        config.set_param("odoo_dashboards_bi.catalog", json.dumps({
            "metrics": candidate.get("metrics", []), "objects": candidate.get("objects", []),
        }))
        config.set_param("odoo_dashboards_bi.pending_release", False)
        return {"release": digest, "status": "active"}
