# Interview talking points (FDSE)

## Numbers to have ready

| | |
|---|---|
| Sources fused | 5 (Companies House, HMRC importers, HMRC trade API, GDACS, UK Sanctions List) + client ERP |
| Ontology | 8 object types · 9 link types · 5 action types |
| Contract checks | 49, run on every build |
| Graph | ~89k nodes, ~223k edges, built in <2 s |
| Entity resolution | v1 95.1% precision / 92.1% recall → v2 98.9% / 97.9% |
| China flood | reaches 192 of 201 suppliers; 82 material |
| Indonesia earthquake | reaches 128; 1 material (a direct steel supplier) |

## 30-second pitch

> "I built a supply-chain resilience app the way an FDSE would at a client. I started from one decision: *when something happens in the world, which of my suppliers are exposed and what are we doing about it?* I modelled that as an ontology of suppliers, companies, commodities, countries, trade flows and events. Then I wrote pipelines that resolve a messy ERP vendor master against Companies House and HMRC data, and built an event-first console where users raise and work alerts that write back into the model. Entity resolution runs at 99% precision, measured against held-out ground truth. Every exposure score can be traced as a path through the graph."

## 2-minute version: structure it like this

1. **Decision:** who the user is and what they need to decide this week.
2. **Ontology:** the two non-obvious modelling calls: `ImportFlow` as an object; `resolves_to` as a scored, overridable link.
3. **The hard part is resolution:** HMRC has no company IDs, and the ERP extract has typos and stripped leading zeros. Blocking → scoring → decision rules, with evidence kept.
4. **The product call:** reachability isn't the point, materiality is. A China event reaches 96% of suppliers but only 41% are material.
5. **Closing the loop:** actions validate, write to writeback, log, and survive rebuilds from source.

## Stories (STAR)

### 1. "My first entity resolver was wrong, and here's how I knew"
- **S:** I needed to link 201 ERP vendors to Companies House records, with no reliable IDs.
- **T:** High precision, because a wrong match puts the wrong company's risk on a real supplier.
- **A:** I generated the ERP extract with a hidden ground-truth file, then measured the resolver against it. v1 hit 95% precision. The failures were all one pattern: "ROGER DYSON LIMITED" matched to "ROGER DYSON **GROUP** LIMITED" at the same postcode. My normaliser stripped GROUP and HOLDINGS as noise, but those words are exactly what distinguish a parent from a subsidiary. A second bug: `token_sort_ratio` scored "MEC OM" vs "MEC COM" at 46 because it sorts words before comparing.
- **R:** I kept the distinguishing words, used the better of two scorers, and added a full-name tie-breaker: 98.9% precision and 97.9% recall. Medium-confidence matches go to a human review queue instead of being silently accepted.
- **Lesson to say out loud:** "Normalisation is a modelling decision. Deciding what counts as noise is domain knowledge."

### 2. "The graph reached everything, which meant it told the user nothing"
- **S:** The first traversal showed a China flood reaching 192 of 201 suppliers.
- **A:** I realised the user's question isn't "is there a path?" but "how much does this matter to us?". I weighted each path by our spend share, the UK import share and event severity, then ranked by a priority that also factors in criticality and financial fragility. The threshold is visible and adjustable in the UI.
- **R:** 82 material suppliers for China, and exactly 1 for the Indonesian earthquake.
- **Why it matters for FDSE:** that was a product decision, not a data one. I made it by thinking about what the user does next.

### 3. "Being honest about what the data can't tell you"
- HMRC says what a company imports but not *where from*. Rather than hide that, the model has two path types: `direct` (overseas vendor located in the affected country) and `inferred` (UK vendor inherits the UK national sourcing mix). Every row and every explanation is labelled with its path type.
- Say: "A decision-maker has to know which numbers are observed and which are estimated, or they can't calibrate how much to trust the tool."

### 4. "The one malformed row in 5.7 million"
- Companies House's header has 4 SIC columns, but one row carries 5, which shifts every later field. DuckDB's CSV reader rejected the file, and a lenient reader would silently mislabel the row. I parse SIC codes by pattern and read the trailing fields from the right.
- Say: "Nightly pipelines break on the row that only appears once. I'd rather handle it deliberately than skip it."

### 5. "Edits must survive the rebuild"
- User decisions (confirmed matches, criticality, alerts) live in writeback tables the pipeline never drops. `apply_edits()` re-applies them over fresh source data. I tested this by confirming a match in the UI, rebuilding everything from source, and checking the edit was still there.

### Link to EY
Prepare one real example from your Oracle work for each of these (use your own details):
- A vendor master with duplicates, bad registration data or inconsistent naming, and what it cost the client.
- A time a report's definition (e.g. "active supplier") differed between two teams, which is the problem an ontology solves.
- A time you had to explain a number's derivation to a non-technical stakeholder, which is the reason for the explainability design here.

## Questions you'll likely get, with answer sketches

**"What's the difference between an ontology and a database schema?"**
A schema describes storage. An ontology describes the business's world: objects, relationships *and the actions allowed on them*, shared across every application. The schema can change underneath without the apps noticing.

**"Why a graph? You could do this in SQL."**
I did both, and a test checks they agree. SQL is fine when the question is fixed. The graph is better when users explore: "what else does this country supply?" or "who else resolves to this company?". Adding a new object type is a YAML change, not a new set of joins.

**"Why NetworkX and not Neo4j?"**
It's reproducible with no infrastructure, and at 89k nodes it's in memory in under 2 seconds. The ontology layer is the part that matters. The graph builder is under 20 lines and generic, so swapping in Neo4j is a backend change, not a redesign. Past a few million edges, or with concurrent users, I'd move.

**"How would you do this at a real client in the first two weeks?"**
Day 1–2: sit with the resilience lead and find the decision they make weekly and what goes wrong. Day 3–5: get the real ERP vendor master and PO history, profile it, and measure how bad resolution is. Week 2: a thin ontology slice (supplier, company, commodity, event), one screen, one action. Ship it to them and watch them use it. Expand only where the user pulls.

**"How would you validate the priority formula?"**
Right now it's a documented judgement call. With real history I'd backtest it: did the suppliers ranked highest during past events actually have late deliveries or shortages? I'd also let users re-rank manually and learn from the difference.

**"The client uses SAP, not Oracle."**
Only the ingest adapter changes (LFA1/LFB1 vendor tables, EKKO/EKPO for POs). The ontology doesn't, which is exactly the point of the ontology.

**"What does Foundry give you that you had to build here?"**
Ontology Manager (my YAML + validator), lineage and health checks (my contract tests), Search Around (my traversal helpers), Actions with permissions and audit (my actions module, minus permissions), Workshop (my Dash app), and multi-user concurrency and security, which I didn't solve. Being specific about this shows you understand the product.

**"What would you do next?"**
Geospatial precision (GDACS polygons against port locations); sanctions screening of vendors and their owners (PSC data); lead-time and inventory-cover objects so the priority reflects days of stock left; and permissioning on actions.

## 5-minute live demo script

1. **Open on the current China flood.** "192 suppliers reached, 82 material. Here's the ranked list."
2. **Click the top supplier. Scroll to 'Why'.** Walk the path: event → country → import flow (64% of UK imports) → commodity → our PO lines → supplier → Companies House record.
3. **Switch to the Indonesia earthquake.** "128 reached, 1 material, a direct steel supplier. Reachability isn't the point."
4. **Raise an alert.** Try raising a duplicate and show the rejection. Try closing without a note and show that rejection.
5. **Match review tab.** "EURY LTD" vs "EBURY LIMITED" at the same postcode. Confirm it, and show the confidence change to 1.0.
6. **Data health tab.** 49/49 contract checks, source quality metrics, and the audit log of what you just did.
7. **Close:** "Rebuild everything from source and these decisions survive, because writeback is separate from source data."
