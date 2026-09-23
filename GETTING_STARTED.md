# Getting started

This guide explains what the project is, how to set it up from scratch, and how to use it. For the design rationale, see [README.md](README.md) and [docs/](docs/).

## What this project is

The **Supply Resilience Console** answers one operational question for a UK manufacturer's supply-chain resilience lead:

> *Something just happened in the world. Which of our suppliers are exposed, why, and what are we doing about it?*

It is built the way Palantir Foundry applications are built:

1. **An ontology** ([`ontology/ontology.yaml`](ontology/ontology.yaml)) defines the business's world. Its 8 object types are Company, Commodity, Country, ImportFlow, RiskEvent, ERPSupplier, PurchaseOrderLine and Alert. It also defines 9 link types between them and 5 actions users can take.
2. **A pipeline** downloads five public datasets, cleans them, resolves messy supplier records to real companies, and loads everything into DuckDB tables that match the ontology. Every build is checked against the ontology (49 contract checks).
3. **A graph** is built from those tables. Exposure is computed by walking it: event → country → import flow → commodity → our purchase orders → our supplier.
4. **A dashboard** (Dash) lets a user pick an event, see which suppliers it seriously affects and why, and act by raising alerts, confirming company matches or setting criticality. Actions are validated, logged, and survive pipeline rebuilds.

### Data sources

| Source | Used for | Access |
|---|---|---|
| [Companies House bulk data](https://download.companieshouse.gov.uk/en_output.html) | Canonical company records, financial-health signals | Free, no key |
| [HMRC importer list](https://www.uktradeinfo.com/trade-data/latest-bulk-datasets/) | Which UK companies import which commodities | Free, no key |
| [HMRC trade statistics API](https://api.uktradeinfo.com) | Where the UK sources each commodity from | Free, no key |
| [GDACS](https://www.gdacs.org) | Current and recent disasters (Orange/Red alerts) | Free, no key |
| [UK Sanctions List](https://www.gov.uk/government/publications/the-uk-sanctions-list) | Countries under UK sanctions regimes | Free, no key |
| Synthetic ERP extract | The "client's" suppliers and purchase orders | Generated locally |

The ERP extract is **synthetic**: it is generated from real importers, then deliberately corrupted (typos, missing registration numbers, duplicate vendors, mixed currencies) to mimic a real Oracle vendor master. A hidden ground-truth file is written alongside it so entity-resolution accuracy can be measured.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python **3.10+** | Developed on 3.11 |
| Internet access | For the first run's downloads (~580 MB) |
| Disk space | **~5 GB free for the first run**, ~2 GB afterwards (the 2.8 GB Companies House CSV is deleted once converted) |
| RAM | 4 GB is enough |
| OS | macOS or Linux; Windows works with the paths adjusted |

No API keys or accounts are needed.

---

## Setup and first run

### 1. Clone and install

```bash
git clone https://github.com/dharun630-cyber/supply-resilience-console.git
cd supply-resilience-console
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

On Windows use `.venv\Scripts\pip` and `.venv\Scripts\python` in place of `.venv/bin/...`.

### 2. Build the data

```bash
.venv/bin/python pipeline.py
```

This takes about **2 minutes** the first time (mostly downloading and parsing 5.7M companies) and about **20 seconds** after that, because downloads are cached in `data/raw/`. It prints each stage, then a summary like:

```
== Object counts ==
  Company              29,796
  Commodity             4,245
  ...
== Entity resolution vs ground truth (ERP) ==
{'vendors': 201, 'predicted_matches': 187, 'precision': 0.989, 'recall': 0.979, ...}

== Validation: 49/49 checks passed ==
```

Exact counts will differ from these as the public sources update. If any validation check fails, the pipeline lists the failures and exits with an error.

### 3. Run the tests

```bash
.venv/bin/python -m pytest -q tests
```

The tests read the database the pipeline just built, so run the pipeline first. They check the ontology contract, that import shares are valid, and that the graph traversal and the SQL implementation of exposure agree.

### 4. Start the dashboard

```bash
.venv/bin/python app/app.py
```

Open **http://127.0.0.1:8050**. Startup takes a few seconds while the graph is built. Use a window at least 1100 px wide to get the three-column layout; narrower windows stack the columns.

---

## Using the dashboard

| Area | What it shows | What you can do |
|---|---|---|
| **Risk events** (left) | Current and recent Orange/Red disasters, each with its count of materially affected suppliers | Click an event to select it; switch between *All recent* and *Current* |
| **Exposure** tab (centre) | Summary tiles, then suppliers ranked by priority, then a diagram and table explaining *why* the selected supplier is exposed | Move the materiality threshold; click a supplier row |
| **Supplier panel** (right) | The ERP record, the Companies House company it resolves to (with match confidence and financial warning signs), and its alerts | Raise an alert, move an alert through its statuses, set criticality, confirm or reject the company match |
| **Alert queue** tab | Every alert, open ones first | Click an alert to open its supplier and event |
| **Match review** tab | Supplier-to-company matches the resolver was unsure about | Click one, then confirm or reject it in the panel |
| **Data health** tab | Contract checks, source data-quality metrics, audit log of actions | Read only |

### How the numbers are calculated

- **Exposure (0–1):** for each commodity we buy from the supplier, our share of spend with them × the event's risk on that commodity.
  - *Direct:* an overseas supplier is exposed if its own country is affected.
  - *Inferred:* a UK supplier inherits the UK's national import mix for that commodity, because HMRC doesn't publish where each company buys from. Every row is labelled with which path applies.
- **Fragility (0–1):** financial warning signs from Companies House filings: not active, accounts or confirmation statement overdue, incorporated under 2 years ago, outstanding charges.
- **Priority (0–1):** `exposure × criticality weight × (1 + fragility) / 2`, where the criticality weights are high 1.0, medium 0.6 and low 0.3.

All weights live in [`src/scm/transform/scoring.py`](src/scm/transform/scoring.py).

### Action rules

- Only one open alert is allowed per supplier and event.
- Alerts move `open → acknowledged → mitigating → closed`. An alert can be closed from any open state, but closing requires a note.
- A confirmed match must be a real Companies House number. Human decisions always override the automatic match.
- Every action is written to `writeback.action_log`, which is shown in the Data health tab.

---

## Configuration

Edit [`src/scm/config.py`](src/scm/config.py):

| Setting | Default | Effect |
|---|---|---|
| `HS_CHAPTERS` | `["29", "72", "84", "85"]` | Commodity chapters in scope: organic chemicals, iron & steel, machinery, electrical equipment |
| `OTS_END_MONTH` | `None` (latest available) | End month (`YYYYMM`) of the 12-month trade window |
| `RANDOM_SEED` | `42` | Seed for the synthetic ERP extract |

After changing the scope, rebuild and regenerate the ERP extract so its purchase orders match the new commodities:

```bash
.venv/bin/python pipeline.py --regen-erp
```

## Common tasks

**Refresh the data.** Downloads are cached by file name. Newer Companies House, HMRC importer and trade-statistics files are fetched automatically when they're published, and GDACS and the sanctions list are fetched once per day. The HMRC commodity and country reference lists (`data/raw/hmrc/commodity_*.json`, `country.json`) are kept until you delete them. To force a full refresh, delete `data/raw/` and rerun the pipeline.

**Reset user actions.** Alerts, match decisions and criticality overrides live in the `writeback` schema. They deliberately survive rebuilds. To clear them, stop the app and run:

```bash
.venv/bin/python -c "import duckdb; c = duckdb.connect('data/warehouse.duckdb'); [c.execute(f'delete from writeback.{t}') for t in ('alert', 'match_override', 'criticality_override', 'action_log')]"
```

**Start completely fresh.** Delete `data/warehouse.duckdb` and rerun the pipeline. This also discards all user actions.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Could not set lock on file ... warehouse.duckdb` | The dashboard and the pipeline are both trying to write to the database; DuckDB allows one writer at a time | Stop the dashboard (Ctrl+C), run the pipeline, restart the dashboard |
| `Address already in use` on port 8050 | Another copy of the dashboard is running | Stop it, or change `port=8050` at the bottom of `app/app.py` |
| Tests error opening `data/warehouse.duckdb` | The pipeline hasn't been run yet | Run `pipeline.py` first |
| `no link matching ... on ...` | A publisher changed its download page | Check the URL in `src/scm/config.py` still lists the file, and update the pattern in the matching `ingest/` module |
| No events under *Current* | GDACS has no active Orange/Red alerts right now | Use *All recent*: past events can be replayed |
| Out of disk during the first run | The Companies House CSV needs ~2.8 GB while it's converted | Free ~5 GB and rerun; completed downloads are reused |

---

## Project layout

```
pipeline.py                   run the full build
ontology/ontology.yaml        the semantic layer (objects, links, actions)
src/scm/
  config.py                   paths, scope, source URLs
  ontology.py                 ontology loader + contract validation
  ingest/                     one module per source (download + clean)
  transform/resolve.py        entity resolution
  transform/build.py          clean → ontology tables; re-applies user edits
  transform/scoring.py        fragility, exposure and priority formulas
  graph.py                    graph builder, traversal, explanations
  actions.py                  validated user actions (writeback)
app/
  app.py                      the dashboard
  service.py                  the dashboard's read/act layer over the ontology
tests/                        contract and consistency tests
docs/                         design notes and interview notes
data/                         created by the pipeline (git-ignored)
```
