/** @odoo-module **/

import { Component, onMounted, useState } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { sprintf } from "@web/core/utils/strings";
import { BiOntologyMap } from "./ontology_map";

const BUSY_STEPS = {
    build_draft: [
        _t("Finding the apps and models you chose…"),
        _t("Reading field metadata from Odoo…"),
        _t("Profiling how each field is filled…"),
        _t("Sending models to the ontology mapper…"),
        _t("Mapping sales, purchasing and inventory…"),
        _t("Mapping accounting, CRM and HR…"),
        _t("Mapping the other apps…"),
        _t("Checking every proposal against your data…"),
        _t("Validating bindings and metrics…"),
        _t("Saving the ontology…"),
        _t("Approving what passed validation…"),
        _t("Publishing the release…"),
    ],
    publish: [
        _t("Assembling the reviewed ontology…"),
        _t("Compiling objects into views…"),
        _t("Provisioning datasets in Odoo…"),
        _t("Activating the release…"),
    ],
    approve_all: [_t("Approving proposals…"), _t("Checking keys against the release…")],
    generate_descriptions: [_t("Reading your objects…"), _t("Writing business descriptions…")],
    reconcile_metrics: [_t("Recomputing baseline metrics…"), _t("Comparing with Odoo…")],
    review: [_t("Saving your decision…")],
    set_ontology_modules: [_t("Saving the apps…")],
    get_status: [_t("Loading the ontology…")],
    default: [_t("Working…")],
};

function arrayValue(value) {
    return Array.isArray(value) ? value : value?.items || [];
}

export class BiOntology extends Component {
    setup() {
        const orm = useService("orm");
        this.notification = useService("notification");
        const caller = orm.silent || orm;
        this.rpc = (method, args) => caller.call("native.bi.ontology", method, args);
        this.state = useState({
            status: null,
            busy: false,
            error: "",
            query: "",
            tab: "metrics",
            descriptionKey: null,
            descriptionText: "",
            modules: null, // the steward's unsaved pick of apps; null = what the server has
            busyText: "",
        });
        onMounted(() => this.reload());
    }

    async run(method, args = [], call = null) {
        if (this.state.busy) {
            return;
        }
        this.state.busy = true;
        this.state.error = "";
        // What the server is doing, a step every few seconds; the last step stays until the call returns.
        const steps = BUSY_STEPS[method] || BUSY_STEPS.default;
        let step = 0;
        this.state.busyText = steps[0];
        const ticker = setInterval(() => {
            step = Math.min(step + 1, steps.length - 1);
            this.state.busyText = steps[step];
        }, 2800);
        try {
            const result = method !== "get_status" ? await (call ? call() : this.rpc(method, args)) : null;
            this.state.status = await this.rpc("get_status", []);
            this.state.error = this.state.status.error || "";
            return result || true; // a call that returns nothing still succeeded
        } catch (error) {
            this.state.error = error?.data?.message || error?.message || _t("The ontology could not be updated.");
            // The banner sits at the top of a long page; a toast shows the refusal where the click happened.
            this.notification.add(this.state.error, { type: "danger" });
            return false;
        } finally {
            clearInterval(ticker);
            this.state.busy = false;
        }
    }

    reload() {
        return this.run("get_status");
    }

    setEnabled() {
        return this.run("set_enabled", [!this.status.enabled]);
    }

    async buildDraft() {
        const result = await this.run("build_draft", [[]]);
        if (result) {
            const approved = Object.values(result.auto?.approved || {}).reduce((sum, count) => sum + count, 0);
            const published = result.publish?.status === "active";
            this.notification.add(published
                ? sprintf(_t("Built and published: %s approved automatically, %s skipped, %s failed validation. Review is optional."),
                    approved, (result.auto?.skipped || []).length, (result.auto?.failed || []).length)
                : sprintf(_t("Built, but not published: %s"), result.publish?.error || _t("the release is pending")),
                { type: published ? "success" : "warning" });
            this.state.tab = "review";
            await this.generateDescriptions();
        }
    }

    get moduleChoices() {
        return arrayValue(this.status.module_choices);
    }

    moduleSelected(name) {
        return this.state.modules ? this.state.modules.includes(name)
            : this.moduleChoices.some((choice) => choice.module === name && choice.selected);
    }

    toggleModule(name) {
        const current = this.moduleChoices.map((choice) => choice.module).filter((module) => this.moduleSelected(module));
        this.state.modules = current.includes(name) ? current.filter((module) => module !== name) : [...current, name];
    }

    get modulesChanged() {
        return Boolean(this.state.modules) && this.moduleChoices.some(
            (choice) => choice.selected !== this.state.modules.includes(choice.module));
    }

    async saveModules() {
        if (await this.run("set_ontology_modules", [this.state.modules])) {
            this.state.modules = null;
            this.notification.add(_t("Saved. Build the ontology draft to map the chosen apps."), { type: "success" });
        }
    }

    get pendingCount() {
        return this.proposals.filter((proposal) => this.needsDecision(proposal)).length;
    }

    canApprove(proposal) {
        // An auto-approved proposal can be approved again: the steward's review replaces the Builder's.
        return (proposal.state !== "approved" || proposal.auto) && proposal.validation?.ok !== false;
    }

    needsDecision(proposal) {
        return proposal.state === "proposed" && proposal.validation?.ok !== false && this.canReview(proposal);
    }

    failed(proposal) {
        return proposal.state === "proposed" && proposal.validation?.ok === false && this.canReview(proposal);
    }

    blockedReason(proposal) {
        // Shown on the card itself; the full list stays in the details.
        const problems = arrayValue(proposal.validation?.problems);
        if (!problems.length) {
            return _t("Can't approve: validation failed");
        }
        const first = this.format(problems[0]);
        return problems.length > 1
            ? sprintf(_t("Can't approve: %s (and %s more)"), first, problems.length - 1)
            : sprintf(_t("Can't approve: %s"), first);
    }

    async approveAll() {
        const result = await this.run("approve_all");
        if (result && typeof result === "object") {
            const skipped = (result.skipped || []).length;
            this.notification.add(skipped
                ? sprintf(_t("Approved %s. %s left for review: open their details for the reason."), result.approved.length, skipped)
                : sprintf(_t("Approved %s proposals."), result.approved.length), { type: skipped ? "warning" : "success" });
        }
    }

    async generateDescriptions() {
        const result = await this.run("generate_descriptions");
        if (result && typeof result === "object") {
            this.notification.add(result.saved
                ? sprintf(_t("Proposed %s object descriptions."), result.saved)
                : _t("Every object already has a description."), { type: result.saved ? "success" : "info" });
        }
        return result;
    }

    editDescription(row) {
        this.state.descriptionKey = row.key;
        this.state.descriptionText = row.description || "";
    }

    async saveDescription() {
        if (await this.run("save_description", [this.state.descriptionKey, this.state.descriptionText])) {
            this.state.descriptionKey = null;
        }
    }

    async review(id, decision) {
        const proposal = this.proposals.find((item) => item.id === id);
        const result = await this.run("review", [id, decision]);
        // Reviews change the draft only: the live release changes when it is published again.
        if (result && proposal?.state === "approved") {
            this.notification.add(decision === "revoked"
                ? _t("Revoked. Publish to drop it from the live release.")
                : _t("Reviewed. Publish to remove the \"not yet reviewed\" note from answers."), { type: "info" });
        }
        return result;
    }

    publish() {
        return this.run("publish");
    }

    retry() {
        return this.run("publish", [this.candidate]);
    }

    rollback() {
        return this.run("publish", [this.previousRelease]);
    }

    get status() {
        return this.state.status || {};
    }

    hash(release) {
        return typeof release === "string" ? release : release?.hash || release?.release || "";
    }

    get release() {
        return this.hash(this.status.release);
    }

    get candidate() {
        return this.hash(this.status.pending_release);
    }

    get previousRelease() {
        const previous = this.hash(this.status.previous_release);
        return previous !== this.release ? previous : "";
    }

    get releaseMismatch() {
        return this.release && this.status.installed_release && this.release !== this.status.installed_release;
    }

    get proposals() {
        return arrayValue(this.status.proposals);
    }

    get proposalGroups() {
        // Objects lead; properties, rules and metrics are grouped by the object they extend; the rest by kind.
        // Pending proposals lead inside each group.
        const order = [
            ["object", _t("Objects")], ["custom_field", _t("Custom fields")],
            ["term", _t("Terms")], ["org_location", _t("Locations")], ["template", _t("Template matches")],
        ];
        const extending = [["property", _t("Properties")], ["rule", _t("Rules")], ["metric", _t("Metrics")]];
        const extendingRank = { property: 0, rule: 1, metric: 2 };
        const rank = { proposed: 0, approved: 1, revoked: 2 };
        const byState = (a, b) => (rank[a.state] ?? 3) - (rank[b.state] ?? 3);
        const counts = (items) => ({
            pending: items.filter((item) => this.needsDecision(item)).length,
            failed: items.filter((item) => this.failed(item)).length,
        });
        const known = new Set(order.map(([kind]) => kind));
        const kindGroups = [...order, ["other", _t("Other")]].map(([kind, label]) => {
            const items = this.proposals
                .filter((proposal) => !(proposal.kind in extendingRank)
                    && (known.has(proposal.kind) ? proposal.kind : "other") === kind)
                .sort(byState);
            return { key: kind, label, items, ...counts(items) };
        }).filter((group) => group.items.length);

        const labels = Object.fromEntries(this.objectRows.map((row) => [row.key, row.label || row.name || row.key]));
        const objects = {};
        for (const proposal of this.proposals.filter((item) => item.kind in extendingRank)) {
            const key = this.proposalObject(proposal) || "";
            (objects[key] = objects[key] || []).push(proposal);
        }
        const objectGroups = Object.entries(objects).map(([key, items]) => {
            items.sort((a, b) => byState(a, b) || extendingRank[a.kind] - extendingRank[b.kind]);
            return {
                key: "object." + key, label: labels[key] || key || _t("No object"), items,
                summary: this.objectSummary(items),
                batch: items.filter((item) => item.batch_approvable && this.needsDecision(item)),
                ...counts(items),
            };
        }).sort((a, b) => a.label.localeCompare(b.label));
        const counters = extending.map(([kind, label]) => {
            const items = this.proposals.filter((item) => item.kind === kind);
            return { kind, label, total: items.length, ...counts(items) };
        }).filter((counter) => counter.total);
        return {
            lead: kindGroups.filter((group) => group.key === "object"),
            rest: kindGroups.filter((group) => group.key !== "object"),
            objects: objectGroups, counters,
        };
    }

    proposalObject(proposal) {
        const payload = proposal.payload || {};
        return payload.object || payload.node?.object || "";
    }

    objectSummary(items) {
        // "+6 dimensions, +2 measures, +3 metrics": what the object gains; revoked proposals don't count.
        const names = {
            dimension: _t("dimensions"), measure: _t("measures"), time: _t("time fields"),
            status: _t("statuses"), rule: _t("rules"), metric: _t("metrics"),
        };
        const totals = {};
        for (const item of items.filter((proposal) => proposal.state !== "revoked")) {
            const name = item.kind === "property" ? names[item.payload?.role] || _t("properties") : names[item.kind];
            totals[name] = (totals[name] || 0) + 1;
        }
        return Object.entries(totals).map(([name, count]) => `+${count} ${name}`).join(", ");
    }

    sourceBadge(proposal) {
        return { infer: _t("Inferred"), llm: _t("AI") }[proposal.payload?.source] || "";
    }

    fillRate(proposal) {
        const rate = proposal.payload?.fill_rate;
        return typeof rate === "number" ? sprintf(_t("%s% filled"), Math.round(rate * 100)) : "";
    }

    ruleText(proposal) {
        return arrayValue(proposal.payload?.when)
            .map((cond) => `${cond.prop} ${cond.op} ${this.format(cond.value)}`).join(_t(" and "));
    }

    async approveBatch(group) {
        // The per-object batch: the review route once per batch-approvable proposal, then one status refresh.
        const ids = group.batch.map((proposal) => proposal.id);
        let approved = 0;
        const result = await this.run("approve_all", [], async () => {
            for (const id of ids) {
                await this.rpc("review", [id, "approved"]);
                approved++;
            }
            return true;
        });
        if (result) {
            this.notification.add(sprintf(_t("Approved %s on %s."), approved, group.label), { type: "success" });
        } else if (approved) {
            this.notification.add(sprintf(_t("Approved %s of %s on %s before the error."), approved, ids.length, group.label),
                { type: "warning" });
        }
    }

    get canPublish() {
        // Querygen compares the reviewed candidate with the live release: only a change is worth publishing.
        return Boolean(this.status.publishable);
    }

    get rows() {
        const list = this.state.tab === "objects" ? this.objectRows : arrayValue(this.status[this.state.tab]);
        const query = this.state.query.toLowerCase().trim();
        return list.filter((row) => [row.key, row.name, row.label, row.description, row.object, row.model, row.source, row.target]
            .some((field) => String(field || "").toLowerCase().includes(query)));
    }

    get objectRows() {
        const active = arrayValue(this.status.objects);
        const keys = new Set(active.map((row) => row.key));
        return [...active, ...arrayValue(this.status.draft_objects).filter((row) => !keys.has(row.key))
            .map((row) => ({ ...row, draft: true }))];
    }

    get drift() {
        return arrayValue(this.status.drift);
    }

    proposalLabel(proposal) {
        return proposal.label || proposal.payload?.label || proposal.node_key || proposal.id;
    }

    canReview(proposal) {
        return !proposal.informational && proposal.kind !== "org_location";
    }

    rowFields(row) {
        const data = row.definition || row;
        return [
            [_t("Object"), row.object],
            [_t("Binding"), row.binding || row.base_model || row.model],
            [_t("Measure"), data.measure],
            [_t("Aggregation"), data.aggregation],
            [_t("Formula"), data.formula || data.derived_from],
            [_t("Unit"), data.unit],
            [_t("Time field"), data.time_property],
            [_t("Dimensions"), data.dimensions],
            [_t("Properties"), data.properties?.map((property) => property.key)],
            [_t("Source"), row.source],
            [_t("Target"), row.target],
            [_t("Lineage"), row.lineage],
            [_t("Trust"), row.badge || row.trust],
        ].filter(([, value]) => value !== null && value !== undefined && value !== "");
    }

    format(value) {
        return typeof value === "string" ? value : JSON.stringify(value, null, 2);
    }
}
BiOntology.template = "odoo_dashboards_bi.BiOntology";
BiOntology.components = { BiOntologyMap };

registry.category("actions").add("native_bi.ontology", BiOntology);
