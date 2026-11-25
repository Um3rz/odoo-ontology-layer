Architecture overview

The project is an Odoo 16 addon stack plus a separate Python service.

Browser ──(Odoo RPC + bus)──▶ Odoo 16 server ──(HTTPS, API key)──▶ Querygen (FastAPI)
                                   │                                   │
                              Odoo Postgres                     Querygen Postgres
                        (business data, BI turns)        (tenants, ontology releases, logs)

- The browser only talks to Odoo. Odoo is the only caller of Querygen.
- Odoo owns the data and access. Every query runs inside Odoo as the user who asked.
- Querygen owns the reasoning. It holds the ontology, decides which tools to run, and writes the answer. It never reads Odoo business data directly.

Odoo side

odoo_dashboards_saas is the base platform. It provides datasets ("analytics"), dashboards, widget renderers (chart, KPI, pivot, table, map, heatmap), alerts, notifications, AI credit accounting and Querygen API credentials.

odoo_dashboards_bi adds the native BI chat and the ontology.
- Data model:
  - native.bi.thread: one conversation per user.
  - native.bi.turn: one question, run as a state machine (queued → running → done / refused / failed / cancelled).
  - native.bi.event: ordered progress events per turn.
- How a turn runs:
  - A cron job picks queued turns with FOR UPDATE SKIP LOCKED and advances each one step at a time.
  - Each turn is capped at 12 steps and 15 minutes.
  - Retries back off on errors from Querygen (HTTP 429 or 5xx).
- Each step:
  a. Odoo sends Querygen the question, recent history, attached context (a record, view or widget) and a catalog of allowed models and metrics.
  b. Querygen replies either with up to 5 tool calls or with a final answer card.
  c. Odoo runs the tools through evidence.execute, in a user-scoped environment (su=False, the user's allowed companies). A refused tool becomes a failed tool result the model can work around.
  d. Odoo sends the results back as the next step.
- Streaming:
  - Answer text deltas go to the browser over bus.bus, sent from a separate database cursor so they arrive before the step's transaction commits.
  - The final card is fetched through an access-checked RPC. Bus messages never carry evidence.
- Evidence and trust:
  - Each tool result is fingerprinted (a SHA-256 of its canonical JSON).
  - A turn also stores a scope key: a hash of the user's groups, companies, record rules and access rights.
  - When a saved answer is shown, Odoo re-runs its evidence as the current user:
    - Access changed (companies, scope, BI disabled, an access error) → hidden.
    - Same access but different data → shown, marked stale.
- Guard rails:
  - Model allowlist (with an option to map extra models).
  - Sensitive models blocked.
  - Personal-data columns excluded from metrics.
  - Every company must have BI enabled and not paused.
  - Credits are charged per answer.
- Ontology management (Odoo models): objects, rules, provisioning, cron jobs (health, garbage collection, daily stock). The setup screen is admin-only.

od_homescreen is the landing page, an OWL client action.
- Pager pages: chat (Discuss), announcements and news (saas notifications), AI chat (registered by the BI module), favourite widgets (rendered with the saas components on gridstack), pinned records, to-do, approvals, HR.
- Optional pages: they register through registry.category("od_homescreen.pages") or are switched on by server-side feature flags (_home_features) based on which models are installed.

Wags apps add HR, contracts, payroll, bonuses and rule-based approvals. The home screen reads them only when they are installed.

FASTAPI aspect:

- Stack: FastAPI, SQLModel and SQLAlchemy on Postgres, Alembic migrations (the ontology tables are migration-only), and LangGraph for agent runs. LLMs are called through OpenAI and Anthropic clients.
- Multi-tenant: a tenant is identified by the Odoo database's UUID and base URL. Installs and licences are checked on every native BI call (API key → install → AI-enabled).
- Ontology pipeline:
  - Templates: YAML per domain (core, sales, purchase, inventory, account, CRM, POS, org) over a catalog of Odoo 16 models and fields.
  - Builder: profiles the tenant's schema and data, infers and matches objects, uses an LLM for proposals, and validated proposals are auto-approved.
  - Compiler: turns specs into compiled queries and signed query tokens.
  - Releases: versioned and published per tenant, with a pointer to the current one.
  - Drift and parity: detect when Odoo's schema diverges from the release.
- Native BI agent: stateless per step. Its state travels back and forth as an opaque blob, sealed with NATIVE_BI_STATE_SECRET, so the server keeps no chat text. It chooses tools over ontology metrics, can run a web search (marked uncertified), and composes the answer card. A rewrite tells Odoo to clear the text it already streamed.
- Agent runtime (other agents, e.g. inventory): LangGraph runs with Redis or Postgres checkpointing, a per-tenant concurrency cap, and an outbox that writes run records back to Odoo. These routes accept a bearer token minted by Odoo and verified against Odoo (whoami), with no cookies or service token.
- Privacy: operational tables hold counts and IDs only, never question or answer text.

Ports

Odoo 16 is the source of truth. A port tool regenerates the 17.0 and 18.0 versions (and a 20.0 tree), handling the API differences between versions.
