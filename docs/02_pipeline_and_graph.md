# Milestone 2: Ingestion pipeline and graph model

## Architecture

```
data/raw/            immutable downloads (cached)
   │  ingest/*.py    parse + type + normalise, one module per source
   ▼
clean.*              one table per source concept (DuckDB)
   │  transform/resolve.py   entity resolution with evidence
   ▼
resolution.*         best match per record: method, score, confidence, needs_review
   │  transform/build.py     + writeback overrides re-applied
   ▼
ontology.*           one table per object type / link type in ontology/ontology.yaml
   │  ontology.validate()    contract checks (PKs, referential integrity, cardinality)
   ▼
graph.build_graph()  generic NetworkX MultiDiGraph (~89k nodes, ~223k edges)

writeback.*          user Actions only; never dropped by the pipeline
```

Run it with `python pipeline.py` (~20 s warm, ~2 min cold), then `pytest -q`.

## Sources as found (September 2026)

| Source | Volume | Real defects handled |
|---|---|---|
| Companies House Basic Company Data | 5,689,367 companies | Header has 4 SIC columns, 1 row has 5, which shifts later columns. Parsed by pattern, not position. Header names have leading spaces. |
| HMRC importers (Jul 2026) | 40,299 in-scope traders | No company number. Traders with more than 50 codes continue onto extra rows. 4 codes not in current nomenclature. |
| HMRC OTS API | 51,538 flows (CN8 × country, 12 m) | Split by port, so aggregated server-side. Non-ISO country codes (XS=Serbia, Dubai/Abu Dhabi/Sharjah). £793m of value has no mappable country. |
| GDACS | 45 Orange/Red events (120 days) | Multi-country events, so modelled as a many-to-many `affects` link. |
| UK Sanctions List | 58k rows | Metadata line above the header. One row per alias, so count distinct IDs. Thematic regimes have no country. |
| Synthetic ERP | 201 vendors, 2,554 PO lines | LTD/LIMITED drift, typos, stripped leading zeros, transposed reg numbers, duplicate vendors, EUR/USD, Oracle `DD-MON-YY` dates. |

## Key design decisions

1. **The ontology is code** (`ontology.yaml`), and it's a contract. The build fails if a primary key duplicates, a link is orphaned, or a many-to-one link fans out.
2. **The graph builder is generic.** Adding an object type to the YAML adds it to the graph with no code change.
3. **Resolution keeps its evidence.** Records aren't merged destructively. Each match stores `match_method`, `match_score` and `confidence`, and a human `confirm` or `reject` in `writeback.match_override` wins on every rebuild.
4. **Denominators include the unmappable.** Import shares divide by *all* UK imports, including confidential or estimated flows, so shares aren't inflated.
5. **Direct vs inferred exposure.** Overseas vendors carry their own country's risk. UK vendors inherit the UK sourcing mix, which is an explicit inference, and it's labelled as one in the output.
6. **Two implementations, one answer.** Exposure is computed by graph traversal (`graph.event_impact`) and by SQL (`scoring.supplier_exposure_sql`), and a test asserts they agree.

## Entity-resolution results (measured against the hidden ground truth)

| Version | Precision | Recall |
|---|---|---|
| v1: `token_sort_ratio` on name with GROUP/HOLDINGS/UK stripped | 0.951 | 0.921 |
| v2: best of `ratio` and `token_sort_ratio`, keep distinguishing words, strip typo'd suffixes, full-name tie-break | **0.989** | **0.979** |

The v1 failure: "ROGER DYSON LIMITED" was matched to "ROGER DYSON **GROUP** LIMITED", its parent company at the same postcode. Stripping "noise" words had erased the one word that told the two apart.

## Known limitations (be upfront about these in interviews)

- **Country granularity.** A flood in one Chinese province "affects China". The next step is intersecting GDACS polygons with port and province locations.
- **Inferred sourcing.** HMRC doesn't publish which country each trader buys from, so UK-vendor exposure is a national average.
- **Trader resolution coverage.** 30.2k of 40.3k traders resolve, and 9.3k of those are medium-confidence. Many unresolved traders are sole traders or partnerships, which Companies House doesn't cover.
- **Materiality.** A China event reaches 192 of 201 suppliers, but only 82 have exposure ≥ 0.10. The dashboard needs thresholds and ranking, not reachability.
