/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";
import { OwlSalesDashboard } from "@odoo_dashboards_saas/components/sales_dashboard";
import { loadElementFilter } from "@odoo_dashboards_saas/components/element_filter_store";

const ACTION = "odoo_dashboards_bi.ask";

patch(OwlSalesDashboard.prototype, "odoo_dashboards_bi.widget_context", {
    setup() {
        this._super(...arguments);
        const handleAction = this.handleContextMenuAction;
        this.handleContextMenuAction = (action, analyticId) => {
            if (action !== ACTION) {
                return handleAction(action, analyticId);
            }
            this.state.contextMenu.visible = false;
            const local = loadElementFilter(analyticId);
            const value = {
                kind: "widget",
                dashboard_id: Number(this.props.action.context.context_dashboard_id),
                widget_id: analyticId,
                range: this.state.lastAppliedRangeFilter,
                filters: this.state.lastAppliedGlobalFilters || [],
                widget_filters: local?.filter || [],
                widget_range: local?.rangeFilter || null,
            };
            this.env.services.native_bi.openWithContext({
                label: this.nativeBiWidgetName || _t("Dashboard widget"),
                value: JSON.parse(JSON.stringify(value)),
            });
        };
    },
    handleRightClick(event, item) {
        const result = this._super(event, item);
        if (!item.__loading && !item.__error && this.getItemTypeById(item.id) !== "sticky_note") {
            this.nativeBiWidgetName = item.name;
            this.state.contextMenu.items.push({
                label: _t("Ask BI about this widget"),
                action: ACTION,
                icon: "fa fa-bar-chart",
            });
        }
        return result;
    },
});
