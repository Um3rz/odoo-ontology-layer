/** @odoo-module **/

import { reactive } from "@odoo/owl";
import { browser } from "@web/core/browser/browser";
import { registry } from "@web/core/registry";
import { _t } from "@web/core/l10n/translation";

const THREAD_MODEL = "native.bi.thread";
const POLL_DELAY = 1500;
const RUNNING = new Set(["queued", "running", "pending"]);

export const nativeBiService = {
    dependencies: ["orm", "bus_service", "user"],
    start(env, { orm, bus_service: bus, user }) {
        const rpc = orm.silent || orm;
        const call = (method, args = []) => rpc.call(THREAD_MODEL, method, args);
        const state = reactive({
            initialized: false,
            loading: false,
            sending: false,
            open: false,
            error: "",
            threads: [],
            thread: null,
            active: null,
            progress: "",
            sources: [],
            streaming: false,
            preview: "",
            draft: "",
            attachment: null,
        });
        let timer;
        let generation = 0;
        let initPromise;
        let cursor = 0;
        let pollInFlight = false;
        let pendingRequest;
        let answerRequest;
        let liveTurn = null; // a turn whose answer text already arrived live

        function errorText(error) {
            return error?.data?.message || error?.message || _t("The request could not be completed.");
        }

        function schedule(delay = POLL_DELAY) {
            browser.clearTimeout(timer);
            if (state.active) {
                timer = browser.setTimeout(poll, delay);
            }
        }

        function showThread(thread) {
            state.thread = thread;
            state.active = thread?.active_turn || null;
            state.progress = "";
            state.sources = [];
            state.streaming = false;
            state.preview = "";
            cursor = 0;
            schedule();
        }

        async function refreshThreads() {
            state.threads = await call("list_threads");
        }

        async function selectThread(id) {
            const selection = ++generation;
            answerRequest?.abort();
            state.loading = true;
            state.error = "";
            browser.clearTimeout(timer);
            try {
                const thread = id ? await call("get_thread", [id]) : null;
                if (selection === generation) {
                    showThread(thread);
                }
            } catch (error) {
                if (selection === generation) {
                    state.error = errorText(error);
                    schedule();
                }
            } finally {
                if (selection === generation) {
                    state.loading = false;
                }
            }
        }

        async function streamAnswer(turnId, selection) {
            const request = new AbortController();
            answerRequest = request;
            state.streaming = true;
            state.preview = "";
            try {
                const params = new URLSearchParams();
                if (user.context.allowed_company_ids?.length) {
                    params.set("companies", user.context.allowed_company_ids.join(","));
                }
                const response = await browser.fetch(`/native-bi/chat/${turnId}/answer?${params}`, {
                    signal: request.signal, credentials: "same-origin",
                });
                if (!response.ok || !response.body || !response.headers.get("Content-Type")?.includes("application/x-ndjson")) {
                    return;
                }
                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                let pending = "";
                try {
                    while (selection === generation) {
                        const { done, value } = await reader.read();
                        if (done || selection !== generation) {
                            break;
                        }
                        const lines = (pending + decoder.decode(value, { stream: true })).split("\n");
                        pending = lines.pop();
                        for (const line of lines) {
                            if (selection !== generation) {
                                return;
                            }
                            state.preview += JSON.parse(line).text;
                            // Network reads can coalesce several chunks into a single frame.
                            if (!document.hidden) {
                                await new Promise((resolve) => browser.setTimeout(resolve, 16));
                            }
                        }
                    }
                } finally {
                    reader.releaseLock();
                }
            } catch {
                // The authoritative transcript remains available via the normal RPC.
            } finally {
                if (answerRequest === request) {
                    answerRequest = null;
                }
            }
        }

        async function poll() {
            const active = state.active;
            const selection = generation;
            if (!active || pollInFlight) {
                return;
            }
            pollInFlight = true;
            try {
                const { events, status } = await call("events_after", [active.id, cursor]);
                if (selection !== generation || state.active?.id !== active.id) {
                    return;
                }
                for (const event of events || []) {
                    cursor = Math.max(cursor, event.sequence);
                    if (event.data?.text && ["queued", "progress"].includes(event.kind)) {
                        state.progress = event.data.text;
                    }
                    if (event.kind === "source" && event.data?.reference) {
                        state.sources.push({ ...event.data, sequence: event.sequence });
                    }
                }
                if (!RUNNING.has(status)) {
                    if ((events || []).some((event) => event.kind === "answer") && liveTurn !== active.id) {
                        await streamAnswer(active.id, selection);
                    }
                    if (selection !== generation) {
                        return;
                    }
                    const thread = await call("get_thread", [state.thread.id]);
                    if (selection === generation && state.active?.id === active.id) {
                        showThread(thread);
                        await refreshThreads();
                    }
                } else {
                    state.active.status = status;
                }
            } catch (error) {
                if (selection === generation && state.active?.id === active.id) {
                    state.error = errorText(error);
                    state.streaming = false;
                }
            } finally {
                pollInFlight = false;
                schedule();
            }
        }

        bus.addEventListener("notification", ({ detail }) => {
            for (const { type, payload } of detail || []) {
                if (type === "native_bi.delta" && payload?.turn_id === state.active?.id) {
                    if (payload.reset) {
                        state.preview = "";
                        liveTurn = null;
                    } else if (payload.text) {
                        state.streaming = true;
                        state.preview += payload.text;
                        liveTurn = payload.turn_id;
                    }
                }
            }
            if (
                state.active &&
                (detail || []).some(({ type, payload }) =>
                    type === "native_bi.progress" && payload?.turn_id === state.active.id
                )
            ) {
                schedule(0);
            }
        });

        function initialize() {
            if (state.initialized) {
                return Promise.resolve();
            }
            if (!initPromise) {
                state.error = "";
                bus.start().catch(() => {});
                initPromise = refreshThreads()
                    .then(() => selectThread(state.threads[0]?.id))
                    .then(() => { state.initialized = !state.error; })
                    .catch((error) => { state.error = errorText(error); })
                    .finally(() => { initPromise = null; });
            }
            return initPromise;
        }

        async function send() {
            const question = state.draft.trim();
            if (!question || state.active || state.sending || state.loading) {
                return;
            }
            state.sending = true;
            state.error = "";
            try {
                if (!state.thread) {
                    showThread(await call("create_thread"));
                }
                const id = state.thread.id;
                const context = state.attachment?.value || {};
                const payloadKey = JSON.stringify([id, question, context]);
                if (pendingRequest?.key !== payloadKey) {
                    pendingRequest = {
                        key: payloadKey,
                        id: Array.from(globalThis.crypto.getRandomValues(new Uint32Array(4))).join("-"),
                    };
                }
                const turn = await call("start_turn", [id, question, pendingRequest.id, context]);
                pendingRequest = null;
                state.active = turn;
                state.draft = "";
                state.attachment = null;
                state.progress = "";
                state.sources = [];
                state.preview = "";
                cursor = 0;
                try {
                    state.thread = await call("get_thread", [id]);
                    await refreshThreads();
                } finally {
                    schedule(0);
                }
            } catch (error) {
                state.error = errorText(error);
            } finally {
                state.sending = false;
            }
        }

        async function cancel() {
            if (!state.active) {
                return;
            }
            try {
                await call("cancel_turn", [state.active.id]);
                schedule(0);
            } catch (error) {
                state.error = errorText(error);
            }
        }

        function openWithContext(attachment) {
            state.attachment = attachment;
            state.open = true;
            initialize();
        }

        return { state, initialize, selectThread, send, cancel, openWithContext };
    },
};

registry.category("services").add("native_bi", nativeBiService);
