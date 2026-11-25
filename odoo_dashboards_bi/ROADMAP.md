# Native BI roadmap

| # | Item | Status | Plan |
|---|---|---|---|
| 1 | Insights page: code-found, optionally AI-narrated feed of business changes, visible after publish, refreshed on a schedule | Planned, next | [INSIGHTS_PLAN.md](INSIGHTS_PLAN.md) |
| 2 | LLM-agnostic providers: per-tenant Anthropic/OpenAI/Gemini keys held in querygen, model choice for the builder and the chat | Planned, parked | querygen-api `docs/LLM_PROVIDERS_PLAN.md` (branch `feature/native-bi`) |
| 3 | Builder full schema coverage: code infers every dimension, measure and standard metric (incl. path dims like warehouse), LLM names and prunes; new `property`/`rule` proposal kinds | Planned | querygen-api `docs/ONTOLOGY_BUILDER_PLAN.md` |

Notes
- Insights narration starts on the server default model and moves to the per-tenant model choice once item 2 ships.

