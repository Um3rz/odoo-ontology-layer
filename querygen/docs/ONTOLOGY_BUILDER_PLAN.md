# Ontology Builder: full schema coverage

Status: plan, 2026-11-22. Not started.

## Goal
The Builder should give the ontology every dimension, time, status and measure that it can read or infer from the Odoo schema, along with the standard metrics on those measures, and not only what the templates ship. The steward still approves every item, but approving should take minutes, not hours.

## Why the Builder misses things today
The checks below are against the code in `app/ontology/builder/`.

| # | Cause | Where |
|---|---|---|
| 1 | **No way to extend an object.** The only proposal kinds are `object`, `metric`, `term` and `custom_field`. A covered or template object can never gain a dimension or measure: `stock_valuation` will never get a warehouse. | `llm.py` TOOL, SYSTEM |
| 2 | **Metrics can only use existing measures and rules.** A new metric must name a measure that is already in the release, and leave its filters empty if no rule exists. | `llm.tool_for`, SYSTEM |
| 3 | **Field truncation.** `domain_input` keeps the first 80 stored fields of each model, sorted custom first and then alphabetically. On wide models (sale.order.line, account.move.line, stock.move) the fields late in the alphabet are dropped without notice. | `llm.domain_input`, `MAX_FIELDS` |
| 4 | **Discovery is left to the LLM.** It decides from metadata which fields matter, so every run's results vary and some get missed. It also sets additivity itself, which produced 8 of the 24 metric failures in the 2026-11-22 run (semi_additive counts that were summed). | SYSTEM |
| 5 | **No paths.** The LLM sees each model on its own, so it never proposes a dotted binding like `stock_move_id.location_dest_id.warehouse_id`, even though the compiler already accepts dotted bindings (`scrapped` uses `stock_move_id.scrapped`). | `domain_input` |
| 6 | **The profile is per field and has no paths.** It holds fill rates for a model's own fields only, so a path dimension can't be checked for real data. | `profile.py`, Odoo profile endpoint |

These two are **not** causes, despite what I said earlier:
- Every domain is asked, not only the domains with templates (`asked = wanted`, and `DEFAULT_DOMAINS` covers all of them).
- Every stored field reaches the LLM, not only those in the fixture (`_fields_with` merges introspect into the compiler fields), apart from the truncation in #3.

## Approach: code discovers, the LLM curates
Swap the roles. A deterministic pass lists every candidate from the schema. The LLM then only names, labels, prunes and adds business meaning on top of that list. Discovery becomes complete and the same on every run. The LLM's output is limited to things that are reviewable.

### 1. Deterministic candidate pass (new `builder/infer.py`)
This pass runs for every object: the release objects (templates included) and the new objects the LLM proposes.

**Own stored fields:**

| Field type | Role | Notes |
|---|---|---|
| many2one | dimension | Bind to the field. The compiler shows the target's display name. |
| selection, boolean | status | Selection values go into the payload for rules. |
| date, datetime | time | The first one with the best fill rate is offered as the metric `time_property`. |
| monetary | measure, unit money | |
| float, integer | measure, unit qty or count | Uses a uom-aware name check: `qty`/`quantity` → qty. Ids, sequences, priority and colour are skipped. |
| char, text, html, binary, one2many, many2many | skipped | There are no values to group by. many2many is left for later. |

**Path dimensions** go one or two many2one hops, but only to a fixed list of conformed dimension targets:
- `stock.warehouse`
- `stock.location`
- `product.product`
- `product.category`
- `res.partner` (and its `country_id`, `state_id`)
- `res.users`
- `crm.team`
- `res.company`
- `uom.uom`
- `account.account`
- `account.analytic.account`
- `hr.department`, `hr.employee`
- `pos.config`
- `project.project`

The pass takes the shortest path to each target, with ties broken by fill rate. For example, `stock.valuation.layer → stock_move_id → warehouse_id` gives the `warehouse` dimension.

**Conformed keys:** the same target gets the same property key on every object (`warehouse`, `product`, `product_category`, `customer`, `salesperson` and so on). Chat can then group any metric by "branch" with a single synonym set. The key comes from the target model (with a role override for partner: customer, vendor or partner, based on the field name). It never comes from the LLM.

**Standard metrics** for each object:
- a record count;
- `sum` for each additive measure;
- `avg` for unit-price-like measures (`price_unit`, `unit_cost`).

Additivity is set by code: an object with `snapshot: true` gets semi_additive with `over_time: last`, and every other object gets additive. Counts are never semi_additive unless the object is a snapshot. This removes cause #4.

**Ranking and caps:**
- Up to 25 dimensions and 15 measures per object, ranked by fill rate and then by conformed-target-first.
- Fields with a fill rate under 5%, or with one distinct value, are dropped and listed in notes, so the steward can see what was left out and why.

### 2. New proposal kinds
- **`property`**: adds a property to an existing object. Payload: `object`, `key`, `label`, `role`, `unit`, `binding` (a dotted path), `synonyms` and `source` (`infer` or `llm`).
- **`rule`**: a named filter on one object, from selection or boolean values. Examples: `done` (`state = done`) and `outgoing` (`picking_type_id.code = outgoing`).
- **`metric`** (existing) may now name a measure or rule that is proposed in the same run. The validator compiles it against the candidate release, with its pending properties and rules included.

### 3. The LLM's new job (`llm.py`)
The LLM's input is the candidate list for each object, not the raw fields. Its output:
- **labels and synonyms** in business language, e.g. `location_dest_id.warehouse_id` → "Branch".
- **drops**: candidates that are technical noise, with a reason.
- **rules** that the selection values suggest.
- **derived metrics**: ratios and differences of existing metrics (margin %, fill rate, late share), using the existing `derived_from`.
- **new objects** for uncovered models, as it does today. Their properties then go through the same infer pass.

Other changes:
- The prompt no longer sets additivity.
- `MAX_FIELDS` truncation is removed, because the input is the ranked candidate list.
- `MAX_PROPOSALS` applies to LLM-only additions. Inferred candidates don't count toward it.

### 4. Validation (`match.py`)
Each `property` candidate is checked in three ways:
- The binding path resolves through introspect, and every hop is a stored many2one.
- The compiler compiles a probe query that groups by the property (the existing `_probe`).
- The fill rate is at or above the threshold, once the profile supports paths (§6).

Each `rule` is checked like this:
- Its values exist in the field's selection.
- The probe compiles.

Each metric also has to pass the existing additivity check. Because code sets additivity, the 2026-11-22 semi_additive failures can't happen again.

### 5. Approval and release merge (`onto_routes/ontology.py`)
- `_release_with_approved` adds approved `property` and `rule` proposals to the target object or release. Template objects get them as an overlay, so a template update keeps them. Overlay entries are keyed by `object.key + property.key`, and a template property with the same key wins (the overlay entry is reported as superseded).
- A key-clash check reuses the `_held_by_other` and `_link_held` pattern for property keys within one object.
- **Batch approval:** inferred candidates with a conformed key, a fill rate of 50% or more and a passing probe are `batch_approvable`. The steward can accept the obvious ones in one click. LLM-only items always need review one by one, as decided in D54.

### 6. Odoo side (`odoo_dashboards_bi`)
- ~~The profile endpoint accepts dotted paths.~~ Dropped on 2026-11-22: querygen approximates a path's fill rate as the product of each hop's fill rate, so Odoo needs no change. If that approximation misleads, add a path profile endpoint later.
- On the review screen, proposals are grouped by object (for example "Stock valuation: +6 dimensions, +2 measures, +3 metrics"), with a "Approve all batch-approvable" button for each object. The existing `Can't approve` and notes lines are reused.

### 7. Known traps (shown as notes, not blocked)
Some paths compile and fill but answer the wrong question. For example, a valuation layer's warehouse is the receiving warehouse, so internal transfers count as 0 for a branch (see the comment in `inventory.yaml`). `infer.py` reads an optional `caveats` mapping from the template YAML (`model.path: note`) and attaches the note to the candidate. These are seeded by hand from the eval findings.
<!-- ponytail: hand-kept caveats; derive them only if the list grows past a few dozen -->

## Outside the Builder (same pass, separate fixes)
- **Product lookup by name.** `lookup_values` in chat only allows properties that published metrics use. It should allow every published `dimension` property with a name binding. This fixes inv-openpo-product.
- **Mixed units of measure.** A qty measure with a uom path gets `unit_dimension: uom`. Chat then refuses to sum across units, or groups by unit instead. This fixes the inv-onhand-branch truth.

## Phases
| Phase | Scope | Size |
|---|---|---|
| A | `infer.py`: own fields, conformed paths, standard metrics, code-set additivity. `property` and `rule` kinds. Validation. Unit tests on fixture introspect. | ~450 lines |
| B | Merge in `_release_with_approved` plus the template overlay. Key-clash check. `batch_approvable` rules. | ~200 lines |
| C | LLM curate mode: new input and output schema, prompt rewrite, truncation removed. | ~200 lines changed |
| D | Odoo: path profile endpoint, and grouped review with "approve all" per object. | ~250 lines |
| E | Chat: wider `lookup_values`, uom guard. Rerun the 20-question eval, then add stock-by-branch and open-PO-by-product to the set. | ~120 lines |

Total: about 1,200 lines and 3–4 days. A and B alone close the stock-by-branch gap and the semi_additive failures, and work without the LLM.

## Done when
- A rebuild on odoo_fresh proposes `warehouse`, `product_category` and `customer`/`vendor` dimensions on every object that can reach them.
- 0 metric proposals fail on additivity.
- Coverage: at least 90% of stored many2one, date and numeric fields with a fill rate of 5% or more, on modelled objects, are a property or are dropped with a reason.
- Eval: inv-stockvalue-branch and inv-openpo-product are answered. No regressions in the 12 that are correct today.
