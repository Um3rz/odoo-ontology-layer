# BI Insights page: plan

Status: plan, 2026-11-19. Not started. See ROADMAP.md.

## What it is
A **Business Intelligence → Insights** screen showing a short ranked feed of what changed in the business, for example "Revenue down 18% vs last month, driven by 3 customers", with the numbers behind each card.
- Visible only once an ontology release is **published** and Insights is **enabled**.
- Regenerated on a schedule (cron) and on demand ("Refresh now").
- **Code finds, AI narrates (optional).** All numbers come from published metrics run through the existing measure/compare/decompose path. The model only writes the words. With AI off, cards use templated text.

## Principles
1. **Deterministic findings.** Detection, ranking and drivers are code. They are reproducible, cheap, and testable against fixtures.
2. **Certified only.** Only published metrics with a time property are used. Exploratory or uncertified queries are never used.
3. **Numeric guard.** Every number in narrated text must appear in the finding (reuse the BI chat answer guard). A card whose narration fails the guard falls back to template text.
4. **Scoped.** A run executes under one configured "run as" user and company, so record rules apply. The page shows each run's scope.

## Settings (Settings → BI, steward only)
| Setting | Default |
|---|---|
| Enable Insights | off |
| Refresh | daily (options: daily, weekly, manual only) |
| Narration | Code only / AI. The AI model is chosen per the LLM providers plan; until then, the server default. `gpt-6.1-terra` is already listed as a config choice in TECHNICAL_PLAN.md. |
| Run as user | the steward who enabled it |
| Comparison | last full month vs the month before (options: week, month, quarter) |
| Max cards | 8 |

Turning Insights off hides the menu and deactivates the cron. Stored runs are kept.

## Generation pipeline (Odoo side, in the cron or the "Refresh now" job)
1. **Candidates:** published metrics with a time property (from the ontology status the module already reads), skipping metrics a steward muted.
2. **Measure:** for each metric, current period vs previous period, using the existing metric execution (`evidence._metric`, compare). Also measure the trailing 6 periods so a normal swing isn't flagged.
3. **Score:** `|pct change| × weight`, where the weight comes from the metric's size relative to its history (z-score over the trailing periods). Drop moves under a threshold (default 10% and z ≥ 1.5). Rank and keep the top N.
4. **Drivers:** for each kept finding, decompose on the metric's main dimensions (customer, product, category, warehouse, whichever the object has) and keep the top 3 contributors. This reuses the decompose tool the chat already uses.
5. **Health hooks (optional):** include high-severity `onto.health` checks as "data quality" cards, since they already run per company on a cron.
6. **Narrate:**
   - **AI:** send findings only (numbers, labels, no rows) to a new querygen route, `POST /onto/insights/narrate`. It reuses the prompt and shape from `app/utils/insights.py` (title, body, severity, metric), is billed through the existing `bill_ai_call`, and its output is checked by the numeric guard.
   - **Code:** template text, e.g. "{metric} {up/down} {pct}% vs {previous period} ({value} vs {prior}). Largest change: {driver} ({delta})."
7. **Store** the run and its cards, then mark it current.

## Data model (Odoo)
- `onto.insight.run`: `company_id`, `run_as_id`, `release_hash`, `period_from`/`period_to`, `compare_from`/`compare_to`, `state` (running/done/failed), `error`, `narration` (code/ai), `model`, `started_at`/`finished_at`.
- `onto.insight`: `run_id`, `sequence`, `metric_key`, `title`, `body`, `severity` (high/medium/info), `value`, `previous`, `pct`, `drivers` (JSON), `series` (JSON trailing values), `evidence` (JSON spec used, so "Open in chat" can replay it), `dismissed_by` (m2m users), `pinned`.
- Record rules: users see runs of their allowed companies. Group: `group_bi_insights` (implied by base user or restricted, to be decided).

## UI: OWL client action `native_bi.insights`
- Header: period ("Oct 2026 vs Sep 2026"), last refreshed time plus run-as scope, **Refresh now** (steward), and a spinner while a run is going.
- Cards ranked by severity then score. Each card shows the headline, a sparkline of the trailing periods (ECharts, already loaded for the ontology graph), the narrated or templated body, and driver chips.
- Card actions: **Ask in BI Chat** (opens chat with the question and metric prefilled), Dismiss (per user), Pin (steward), Mute metric (steward).
- Empty states: no published ontology ("Publish the ontology to enable Insights"), disabled, no significant changes, last run failed (with the error).

## Menu visibility ("only after the ontology is published")
Odoo 16 menus can't depend on data, so the `menu_native_bi_insights` record's `active` is toggled:
- turned on when a publish succeeds **and** Insights is enabled; turned off on disable;
- re-synced by the insights cron and at module upgrade (a post-init hook or a function call in data XML), since an XML update can reset it.

The action also checks this itself and shows the empty state, so a stale menu never shows wrong data.

## Querygen changes
- `POST /onto/insights/narrate`: onto auth (`registered_tenant`), body `{findings, period, locale}`, returns `{items, usage}`. It reuses the `insights.narrate` prompt and moves it off the hardcoded Anthropic call onto the provider layer when that ships. Until then it uses the existing client.
- No new storage: querygen stays stateless for insights.

## Phases
1. **Code-only insights:** models, settings, cron, pipeline steps 1–4 plus templated text, the page, and menu toggling. Tests: scoring and thresholds on fixture series; a pipeline run on `odoo_fresh` fixture data against known expected movers (reuse `evals/onto` truth SQL where it applies).
2. **AI narration:** the querygen route, the guard, billing, a fallback to templates on failure.
3. **Actions:** Ask in chat, dismiss, pin, mute, health cards.
4. **Model choice:** wire narration into the LLM providers plan (per-tenant model).

## Open decisions
- Who can see Insights: all internal users, or a new group? It defaults to the same group as BI Chat.
- Run-as user versus per-viewer generation. Per-viewer is fully scoped but costs N× more; the plan uses run-as plus a company scope.
- Should data-quality (health) cards appear in the same feed, or in a separate tab?

## Size
About 1,000–1,300 lines: Odoo models, cron and pipeline (~450), the OWL page (~350), the querygen route (~80), and tests (~300). Phase 1 can ship on its own.
