/** @odoo-module **/

import { onMounted, onWillUnmount, reactive, useRef } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";
import { FormController } from "@web/views/form/form_controller";
import { GraphController } from "@web/views/graph/graph_controller";
import { KanbanController } from "@web/views/kanban/kanban_controller";
import { ListController } from "@web/views/list/list_controller";
import { PivotController } from "@web/views/pivot/pivot_controller";

const targets = new Set();
export const viewTarget = reactive({ current: null });

function savedRecord(controller, element) {
    const record = controller.model.root;
    if (!record?.resId || record.isVirtual) {
        return null;
    }
    const model = record.resModel;
    const label = String(record.data.display_name || record.data.name || model);
    if (model === "ai.insight.saas") {
        return {
            label: String(record.data.title || label),
            value: { kind: "insight", insight_id: record.resId },
        };
    }
    const pageFields = element ? Array.from(element.querySelectorAll(".o_field_widget[name]"))
        .filter((field) => field.getClientRects().length && !field.parentElement.closest(".o_field_widget"))
        .map((field) => field.getAttribute("name")) : null;
    return { label, value: { kind: "record", model, record_ids: [record.resId],
        view_type: "form", view_id: controller.props.viewId,
        page_fields: pageFields && [...new Set(pageFields)].slice(0, 100) } };
}

function view(controller, type) {
    const root = controller.model?.root;
    const model = root?.resModel || controller.props.resModel;
    if (!model) {
        return null;
    }
    const domain = controller.env.searchModel?.domain || controller.props.domain || [];
    const value = { kind: "view", view_type: type, view_id: controller.props.viewId, model, domain };
    if (type === "list" && !root?.isDomainSelected && root?.selection?.length) {
        value.record_ids = root.selection.map((record) => record.resId);
        return { label: _t("Selected records"), value };
    }
    if (type === "pivot") {
        const { pivot_measures, pivot_row_groupby, pivot_column_groupby } = controller.getContext();
        value.measures = pivot_measures;
        value.groupby = [...new Set([...pivot_row_groupby, ...pivot_column_groupby])];
    }
    if (type === "graph") {
        const { graph_measure, graph_groupbys } = controller.getContext();
        value.measures = graph_measure ? [graph_measure] : [];
        value.groupby = graph_groupbys;
    }
    return { label: controller.env.config.displayName || model, value };
}

for (const [Controller, type] of [
    [FormController, "form"],
    [ListController, "list"],
    [KanbanController, "kanban"],
    [PivotController, "pivot"],
    [GraphController, "graph"],
]) {
    patch(Controller.prototype, "odoo_dashboards_bi.view_context", {
        setup() {
            this._super(...arguments);
            const root = type === "form" ? useRef("root") : null;
            const build = () => (root ? savedRecord(this, root.el) : view(this, type));
            onMounted(() => {
                targets.add(build);
                viewTarget.current = build;
            });
            onWillUnmount(() => {
                targets.delete(build);
                if (viewTarget.current === build) {
                    viewTarget.current = Array.from(targets).pop() || null;
                }
            });
        },
    });
}
