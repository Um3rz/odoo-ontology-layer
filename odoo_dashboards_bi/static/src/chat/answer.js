/** @odoo-module **/

import { Component, useEffect, useRef } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

export class BiChart extends Component {
    setup() {
        const element = useRef("chart");
        let chart;
        let observer;
        useEffect((container, data) => {
            if (!container || !globalThis.echarts) {
                return;
            }
            chart = globalThis.echarts.init(container);
            const theme = getComputedStyle(container);
            const textColor = theme.color;
            const accent = theme.getPropertyValue("--bi-accent").trim();
            const guide = theme.getPropertyValue("--bi-line").trim();
            chart.setOption({
                textStyle: { color: textColor },
                grid: { top: data.series.length > 1 ? 38 : 14, bottom: 12, left: 12, right: 12, containLabel: true },
                tooltip: { trigger: "axis", renderMode: "richText" },
                legend: { show: data.series.length > 1, textStyle: { color: textColor } },
                xAxis: { type: "category", data: data.x.values, axisLabel: { color: textColor, hideOverlap: true },
                    axisTick: { show: false }, axisLine: { lineStyle: { color: guide } } },
                yAxis: { type: "value", axisLabel: { color: textColor }, splitLine: { lineStyle: { color: guide } } },
                series: data.series.map((series, index) => ({
                    name: series.label || series.key,
                    type: data.kind === "line" ? "line" : "bar",
                    data: series.values,
                    barMaxWidth: 38,
                    itemStyle: { ...(index === 0 && accent ? { color: accent } : {}), borderRadius: [4, 4, 0, 0] },
                })),
            });
            if (globalThis.ResizeObserver) {
                observer = new ResizeObserver(() => chart?.resize());
                observer.observe(container);
            }
            return () => {
                observer?.disconnect();
                chart?.dispose();
                chart = null;
            };
        }, () => [element.el, this.props.chart]);
    }
}
BiChart.template = "odoo_dashboards_bi.BiChart";
BiChart.props = { chart: Object };

export class BiAnswer extends Component {
    setup() {
        this.action = useService("action");
    }

    get card() {
        return this.props.card;
    }

    get sources() {
        return this.card.sources?.length ? this.card.sources : (this.card.source ? [this.card.source] : []);
    }

    get chart() {
        const chart = this.card.chart;
        return chart?.x?.values?.length && chart.series?.length && globalThis.echarts ? chart : null;
    }

    get numbers() {
        return Array.isArray(this.card.numbers) ? this.card.numbers : [];
    }

    columnLabel(column) {
        const key = column.key || column;
        if (key === "x" && (!column.label || column.label === "x")) {
            return "Category";
        }
        if (key === "y" && (!column.label || column.label === "y")) {
            const definition = this.card.definition;
            return (typeof definition === "string" ? definition : definition?.label) || this.card.chart?.title || "Value";
        }
        return column.label || key;
    }

    sourceLabel(source) {
        return source.label || source.metric || source.source_id || source.model || source.kind;
    }

    sourceUrl(source) {
        try {
            const url = new URL(source.url);
            return url.protocol === "https:" ? url.href : null;
        } catch {
            return null;
        }
    }

    sourceRecord(source) {
        return source.model && source.record_ids?.length === 1
            ? Number(source.record_ids[0])
            : 0;
    }

    openSource(source) {
        const recordId = this.sourceRecord(source);
        if (recordId) {
            this.action.doAction({
                type: "ir.actions.act_window",
                res_model: source.model,
                res_id: recordId,
                views: [[false, "form"]],
                target: "current",
            });
        }
    }

    formatCell(value) {
        if (value === null || value === undefined || value === false) {
            return value === false ? "No" : "—";
        }
        if (value === true) {
            return "Yes";
        }
        if (typeof value === "number") {
            return new Intl.NumberFormat(undefined, { maximumFractionDigits: 6 }).format(value);
        }
        return Array.isArray(value) ? value[1] ?? value.join(", ") : String(value);
    }
}
BiAnswer.template = "odoo_dashboards_bi.BiAnswer";
BiAnswer.components = { BiChart };
BiAnswer.props = { card: Object };
