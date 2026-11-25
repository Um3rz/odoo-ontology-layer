/** @odoo-module **/

import { Component, onMounted, useEffect, useExternalListener, useRef, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";
import { useService } from "@web/core/utils/hooks";
import { BiAnswer } from "./answer";
import { viewTarget } from "./view_context";

const SOURCE_ACTIONS = {
    search_web: _t("Searched public documentation"),
    attachment: _t("Read attachment"), measure: _t("Queried ontology metric"),
    compare: _t("Compared periods"), lookup_values: _t("Looked up a dimension"),
    explore: _t("Explored Odoo data"), read_group: _t("Grouped Odoo records"),
    search_count: _t("Counted Odoo records"), search_read: _t("Searched Odoo records"),
    read_records: _t("Read Odoo records"),
};

class BiActivity extends Component {
    toolLabel(source) {
        return SOURCE_ACTIONS[source.tool] || _t("Read source");
    }
}
BiActivity.template = "odoo_dashboards_bi.BiActivity";
BiActivity.props = { sources: Array, live: { type: Boolean, optional: true } };

export class BiChat extends Component {
    setup() {
        this.bi = useService("native_bi");
        this.action = useService("action");
        this.user = useService("user");
        this.access = useState({ canManageOntology: false });
        this.layout = useState({ recentExpanded: true });
        this.prompts = [
            _t("Compare gross revenue for the two most recent complete months"),
            _t("Which products sold the most units last month?"),
            _t("Show usable stock by warehouse"),
        ];
        this.state = useState(this.bi.state);
        this.viewTarget = useState(viewTarget);
        const transcript = useRef("transcript");
        const compose = useRef("compose");
        onMounted(async () => {
            if (this.props.drawer) {
                this.focusComposer();
            }
            this.bi.initialize();
            this.access.canManageOntology = await this.user.hasGroup("base.group_system");
        });
        useEffect((element) => {
            if (element) {
                const latest = element.querySelector(".o_native_bi_answer_preview") ||
                    element.querySelector(".o_native_bi_message:last-of-type");
                const start = latest && latest.getBoundingClientRect().top - element.getBoundingClientRect().top;
                element.scrollTop = latest ? element.scrollTop + start - 12 : 0;
            }
        }, () => [transcript.el, this.state.thread?.id, this.messages.length, Boolean(this.state.preview)]);
        this.focusComposer = () => compose.el?.focus();
    }

    get messages() {
        return this.state.thread?.messages || [];
    }

    get recentToggleLabel() {
        return this.layout.recentExpanded ? _t("Hide recent chats") : _t("Show recent chats");
    }

    toggleRecent() {
        this.layout.recentExpanded = !this.layout.recentExpanded;
    }

    get canSend() {
        return this.state.initialized && !this.state.sending && !this.state.loading &&
            !this.state.active && Boolean(this.state.draft.trim());
    }

    get currentContext() {
        return this.props.drawer ? this.viewTarget.current?.() : null;
    }

    get attachLabel() {
        return this.currentContext?.value.kind === "record" ?
            _t("Attach open record") : _t("Attach current page");
    }

    send() {
        if (this.canSend) {
            this.bi.send();
        }
    }

    onComposeKeydown(event) {
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
            event.preventDefault();
            this.send();
        }
    }

    onThreadChange(event) {
        return this.selectThread(Number(event.target.value));
    }

    selectThread(id) {
        this.state.draft = "";
        this.state.attachment = null;
        return this.bi.selectThread(id);
    }

    newChat() {
        this.bi.selectThread(null);
        this.state.draft = "";
        this.state.attachment = null;
        this.focusComposer();
    }

    usePrompt(prompt) {
        this.state.draft = String(prompt);
        this.focusComposer();
    }

    openOntology() {
        this.state.open = false;
        return this.action.doAction("odoo_dashboards_bi.action_native_bi_ontology");
    }

    addContext() {
        const context = this.currentContext;
        if (context) {
            this.state.attachment = {
                label: context.label,
                value: JSON.parse(JSON.stringify(context.value)),
            };
            this.focusComposer();
        }
    }
}
BiChat.template = "odoo_dashboards_bi.BiChat";
BiChat.components = { BiAnswer, BiActivity };
BiChat.props = { drawer: { type: Boolean, optional: true } };

export class BiHomePage extends Component {}
BiHomePage.template = "odoo_dashboards_bi.BiHomePage";
BiHomePage.components = { BiChat };
BiHomePage.props = {};

class BiChatAction extends BiHomePage {}
BiChatAction.template = "odoo_dashboards_bi.BiChatAction";
BiChatAction.props = ["*"];

export class BiDrawer extends Component {
    setup() {
        this.state = useState(useService("native_bi").state);
        this.styleAdapter = registry.category("od_homescreen.styles").get("home", null);
        if (this.styleAdapter) {
            // Dark comes from html.o_od_dark (home theme service) through the .o_glass_drawer tokens.
            this.theme = useState({ style: this.styleAdapter.loadStyle() });
            useExternalListener(window, this.styleAdapter.STYLE_EVENT, (event) => { this.theme.style = event.detail; });
            useExternalListener(window, "storage", (event) => {
                if (event.key === this.styleAdapter.STORAGE_KEY || event.key === null) {
                    this.theme.style = this.styleAdapter.loadStyle();
                }
            });
        }
    }

    get palette() {
        if (!this.styleAdapter) return "";
        const id = this.theme.style.palette;
        return `${this.styleAdapter.paletteCss(id)}; --g-on-primary: ${this.styleAdapter.primaryText(id)}`;
    }

    get normal() {
        return Boolean(this.theme && this.theme.style.theme === "normal");
    }

    open() {
        this.state.open = true;
    }

    close() {
        this.state.open = false;
    }

    onKeydown(event) {
        if (event.key === "Escape") {
            this.close();
        }
    }
}
BiDrawer.template = "odoo_dashboards_bi.BiDrawer";
BiDrawer.components = { BiChat };

registry.category("actions").add("native_bi.chat", BiChatAction);
registry.category("main_components").add("native_bi.drawer", { Component: BiDrawer });

registry.category("od_homescreen.pages").add("bi-ai", BiHomePage);
