/** @odoo-module **/

import { Component, useEffect, useRef, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

function indexBy(items, key) {
    const index = new Map();
    for (const item of items) {
        const value = item[key];
        if (!index.has(value)) {
            index.set(value, []);
        }
        index.get(value).push(item);
    }
    return index;
}

export function taxonomyRows(status) {
    const domains = [...(status.domains || [])];
    const known = new Set(domains.map((domain) => domain.key));
    const objects = status.objects || [];
    const metrics = indexBy(status.metrics || [], "object");
    const placedMetrics = indexBy((status.metrics || []).filter((metric) =>
        known.has(metric.domain) && metric.domain !== objects.find((object) => object.key === metric.object)?.domain
    ), "domain");
    const classified = new Map();
    for (const object of objects) {
        const key = object.domain || "other";
        if (!known.has(key)) {
            domains.push({ key, label: key.split(".").pop().replace(/_/g, " ") });
            known.add(key);
        }
        const row = { ...object, metrics: (metrics.get(object.key) || []).filter((metric) =>
            !known.has(metric.domain) || metric.domain === key) };
        if (!classified.has(key)) {
            classified.set(key, []);
        }
        classified.get(key).push(row);
    }
    const hierarchy = indexBy(domains.map((domain) => ({ ...domain,
        parent: known.has(domain.parent) ? domain.parent : null,
    })), "parent");
    function branch(domain) {
        const children = (hierarchy.get(domain.key) || []).map(branch).filter(Boolean);
        const members = classified.get(domain.key) || [];
        const measures = placedMetrics.get(domain.key) || [];
        return children.length || members.length || measures.length ? { ...domain, children, members, measures } : null;
    }
    return (hierarchy.get(null) || []).map(branch).filter(Boolean);
}

function treeNode(group) {
    return { name: group.label, children: [
        ...group.children.map(treeNode),
        ...group.measures.map((metric) => ({ name: metric.label, objectKey: metric.object, metricKey: metric.key })),
        ...group.members.map((object) => ({ name: object.label, objectKey: object.key,
            children: object.metrics.map((metric) => ({ name: metric.label, objectKey: object.key, metricKey: metric.key })),
        })),
    ] };
}

export class BiOntologyMap extends Component {
    setup() {
        this.state = useState({ simple: false, selected: "", metric: "", root: "", lineage: null,
            showLineage: false, lineageError: false, showUnlinked: false });
        this.orm = useService("orm");
        const chartRef = useRef("chart");
        useEffect((element, view, status, root, lineage, showUnlinked) => {
            if (!element || !globalThis.echarts) {
                return;
            }
            const chart = globalThis.echarts.init(element);
            const theme = getComputedStyle(element);
            const ink = theme.color;
            const accent = theme.getPropertyValue("--bi-accent").trim();
            const options = view === "taxonomy" ? {
                type: "tree", left: "6%", right: "27%", top: "6%", bottom: "6%", symbolSize: 8,
                expandAndCollapse: true, initialTreeDepth: 2, label: { color: ink, position: "top" },
                leaves: { label: { color: ink, position: "right" } }, itemStyle: { color: accent },
                data: [{ name: "Business domains", children: taxonomyRows(status).map(treeNode) }],
            } : this.graphSeries(ink, accent, status, root, lineage, showUnlinked);
            chart.setOption({ tooltip: { renderMode: "richText" }, series: [options],
                legend: options.categories?.length > 1 ? { type: "scroll", top: 0, textStyle: { color: ink },
                    data: options.categories.map((category) => category.name) } : undefined });
            chart.on("click", (event) => {
                if (event.data?.objectKey) {
                    this.select(event.data.objectKey, event.data.metricKey);
                }
            });
            const observer = new ResizeObserver(() => chart.resize());
            observer.observe(element);
            return () => {
                observer.disconnect();
                chart.dispose();
            };
        }, () => [chartRef.el, this.props.view, this.props.status, this.state.root,
            this.state.showLineage ? this.state.lineage : null, this.state.showUnlinked]);
    }

    async toggleLineage(event) {
        this.state.showLineage = event.target.checked;
        if (this.state.showLineage && !this.state.lineage) {
            this.state.lineageError = false;
            try {
                this.state.lineage = await this.orm.call("native.bi.ontology", "dashboard_lineage");
            } catch {
                this.state.lineageError = true;
            }
        }
    }

    select(object, metric = "") {
        this.state.selected = object;
        this.state.metric = metric;
    }

    graphSeries(ink, accent, status, root, lineage, showUnlinked = false) {
        const links = [...(status.links || []), ...(status.navigation_links || [])];
        const nodes = [...(status.objects || [])];
        const add = (key, label) => {
            if (!nodes.some((node) => node.key === key)) {
                nodes.push({ key, label, lineage: true });
            }
        };
        for (const dashboard of lineage || []) {
            const source = "dashboard:" + dashboard.id;
            add(source, dashboard.label);
            for (const widget of dashboard.widgets) {
                const widgetKey = "widget:" + widget.id;
                add(widgetKey, widget.label);
                links.push({ source, target: widgetKey, label: "Contains" });
                if (widget.dataset) {
                    const datasetKey = "dataset:" + widget.dataset.id;
                    add(datasetKey, widget.dataset.label);
                    links.push({ source: widgetKey, target: datasetKey, label: "Uses dataset" });
                    const mapped = widget.dataset.object || status.objects.find((object) =>
                        object.base_model === widget.dataset.model)?.key;
                    if (mapped) {
                        links.push({ source: datasetKey, target: mapped, label: "Covers business object" });
                    } else if (widget.dataset.model) {
                        const model = "model:" + widget.dataset.model;
                        add(model, widget.dataset.model);
                        links.push({ source: datasetKey, target: model, label: "Reads Odoo model" });
                    }
                }
            }
        }
        const neighbors = new Set([root]);
        for (const link of links) {
            if (link.source === root || link.target === root) {
                neighbors.add(link.source);
                neighbors.add(link.target);
            }
        }
        const scoped = nodes.filter((object) => !root || neighbors.has(object.key));
        const scopedKeys = new Set(scoped.map((object) => object.key));
        const shownLinks = links.filter((link) => scopedKeys.has(link.source) && scopedKeys.has(link.target)
            && link.source !== link.target);
        const degree = new Map();
        for (const link of shownLinks) {
            degree.set(link.source, (degree.get(link.source) || 0) + 1);
            degree.set(link.target, (degree.get(link.target) || 0) + 1);
        }
        const objects = scoped.filter((object) => showUnlinked || object.key === root || degree.get(object.key));
        // Colour by top-level business domain so the legend stays short and toggles whole areas.
        const domains = new Map((status.domains || []).map((domain) => [domain.key, domain]));
        const topDomain = (key) => {
            const seen = new Set();
            while (domains.get(key)?.parent && domains.has(domains.get(key).parent) && !seen.has(key)) {
                seen.add(key);
                key = domains.get(key).parent;
            }
            return key;
        };
        const categoryOf = (object) => object.lineage ? "Dashboard lineage"
            : domains.get(topDomain(object.domain))?.label || (object.domain ? topDomain(object.domain) : "Other");
        const categories = [...new Set(objects.map(categoryOf))];
        const counts = new Map();
        for (const object of objects) {
            counts.set(object.label, (counts.get(object.label) || 0) + 1);
        }
        const showLabels = objects.length <= 25;
        return { type: "graph", layout: "force", roam: true, draggable: true,
            force: { repulsion: 300, edgeLength: [80, 150], gravity: 0.1, layoutAnimation: true },
            categories: categories.length > 1 ? categories.map((name) => ({ name })) : [{ name: categories[0], itemStyle: { color: accent } }],
            label: { color: ink, position: "right" }, labelLayout: { hideOverlap: true },
            edgeSymbol: ["none", "arrow"], edgeSymbolSize: 7, lineStyle: { color: ink, opacity: 0.25, curveness: 0.1 },
            emphasis: { focus: "adjacency", label: { show: true }, lineStyle: { opacity: 0.8 } },
            data: objects.map((object) => {
                const links = degree.get(object.key) || 0;
                return { id: object.key, objectKey: object.lineage ? undefined : object.key,
                    name: counts.get(object.label) > 1 ? `${object.label} (${object.base_model || object.key})` : object.label,
                    category: categories.indexOf(categoryOf(object)), value: links,
                    symbolSize: Math.min(10 + 3 * links, 40), symbol: object.lineage ? "roundRect" : "circle",
                    label: { show: showLabels || links >= 4 || object.key === root } };
            }),
            links: shownLinks.map((link) => ({ source: link.source, target: link.target, name: link.label,
                    lineStyle: link.navigation_only ? { type: "dashed" } : undefined })),
        };
    }

    get objects() { return this.props.status.objects || []; }
    get canDraw() { return Boolean(globalThis.echarts); }
    get taxonomy() { return taxonomyRows(this.props.status); }
    get focused() { return this.objects.find((object) => object.key === this.state.selected); }
    get focusedMetrics() {
        return (this.props.status.metrics || []).filter((metric) => metric.object === this.state.selected);
    }
    get selectedMetric() { return this.focusedMetrics.find((metric) => metric.key === this.state.metric); }
    metricDefinition(metric) {
        return metric.derived_from || [metric.aggregation, metric.measure].filter(Boolean).join(" · ");
    }
    conditionText(condition) {
        return [condition.prop, condition.op, Array.isArray(condition.value) ? condition.value.join(", ") : condition.value].join(" ");
    }
    get visibleObjects() {
        return this.state.root ? this.objects.filter((object) => object.key === this.state.root) : this.objects;
    }

    connections(object) {
        const labels = new Map(this.objects.map((item) => [item.key, item.label]));
        const links = [...(this.props.status.links || []), ...(this.props.status.navigation_links || [])];
        return links.filter((link) => link.source === object.key || link.target === object.key)
            .map((link) => ({ ...link, neighbor: labels.get(link.source === object.key ? link.target : link.source) }));
    }
}

BiOntologyMap.template = "odoo_dashboards_bi.BiOntologyMap";
BiOntologyMap.props = { view: String, status: Object };
