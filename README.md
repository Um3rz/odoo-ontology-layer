# odoo-ontology-layer

An ontology layer for Odoo 16 and a chat that answers business questions from it.

| Folder | What it is |
|---|---|
| `odoo_dashboards_bi/` | Odoo 16 addon. Ontology builder and the native BI chat: a question runs as a turn whose steps call Querygen, and every query runs inside Odoo, as the asking user, under their access rights. |
| `querygen/` | FastAPI service. Builds and publishes the ontology (templates, inference, drift, parity), and drives each chat step: it asks the model which Odoo tools to run and writes the answer card. It never reads Odoo data directly. |

## How a question flows

1. The user asks in the Odoo chat. The addon creates a turn and posts it to `querygen` at `native-bi/v1/step`.
2. Querygen returns tool calls over ontology metrics (or the final answer card).
3. The addon runs each tool in Odoo as that user and sends the results back as the next step.
4. The answer is saved with a fingerprint of the evidence behind it. Chat history re-checks it on load: it is hidden if the user's access changed, and marked stale if only the data changed.

## Run Querygen

```sh
cd querygen
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # fill in DATABASE_URL, INTERNAL_API_KEY, NATIVE_BI_STATE_SECRET, OPENAI_API_KEY
alembic upgrade head            # Onto tables
uvicorn app.main:app --port 8000
```

Tests need a Postgres server (`DATABASE_URL`); each run creates and drops a scratch database:

```sh
python -m pytest tests/onto
```

## Install the addon

Copy `odoo_dashboards_bi` into your Odoo 16 addons path. It depends on `odoo_dashboards_saas` (the Odoo Dashboards app, https://www.odoodashboards.com), plus `bus`, `mail` and `stock`. Point it at your Querygen service with two system parameters, `odoo_dashboards_bi.querygen_url` and `odoo_dashboards_bi.querygen_key` (without them it uses the Odoo Dashboards cloud credentials), then enable BI for your companies.

## License

The addon is LGPL-3, as its manifest declares.
