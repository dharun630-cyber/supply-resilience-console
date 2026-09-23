"""Entity resolution: map messy records (HMRC traders, ERP vendors) to canonical Companies.

Classic three-stage design:
  1. BLOCKING   - cheaply generate candidate pairs (same reg number / same postcode / same
                  normalised name) instead of comparing against all 5.7M companies.
  2. SCORING    - compare names of each candidate pair (best of rapidfuzz ratio and token_sort_ratio on the
                  'core' name with legal suffixes stripped).
  3. DECISION   - rules turn (method, score) into a confidence; keep the best candidate.

Every decision keeps its evidence (method, score, confidence). Nothing is merged
destructively, so a human can override a match later via an Action.
"""
from __future__ import annotations

import duckdb
import pandas as pd
from rapidfuzz import fuzz

# (method, min_score) -> confidence. Ordered strongest first.
RULES = [
    # A stated reg number is strong evidence; the name check only has to catch
    # transposed digits that point at an unrelated company.
    ("registration_number", 70, 0.99),
    ("postcode_name", 92, 0.95),
    ("exact_name", 100, 0.85),
    ("postcode_name", 80, 0.75),
]
ACCEPT_AT = 0.75
REVIEW_BELOW = 0.90  # accepted but flagged for human confirmation


def _candidates(con: duckdb.DuckDBPyConnection, records_sql: str) -> pd.DataFrame:
    """records_sql must yield: rec_id, name_norm, name_core, postcode_norm, stated_company_number."""
    return con.execute(f"""
    with r as ({records_sql}),
    by_number as (
        select r.rec_id, c.company_number, 'registration_number' as method
        from r join clean.companies c on c.company_number = r.stated_company_number
    ),
    by_postcode as (
        select r.rec_id, c.company_number, 'postcode_name' as method
        from r join clean.companies c on c.postcode_norm = r.postcode_norm
        -- cheap prefilter; business-centre postcodes can hold thousands of companies
        where jaro_winkler_similarity(c.name_core, r.name_core) >= 0.80
    ),
    by_name as (
        select r.rec_id, c.company_number, 'exact_name' as method
        from r join clean.companies c on c.name_norm = r.name_norm
    )
    select x.rec_id, x.method, c.company_number, c.name_core as cand_core, c.status,
           r.name_core as rec_core, c.name_norm as cand_full, r.name_norm as rec_full
    from (select * from by_number union all select * from by_postcode union all select * from by_name) x
    join r using (rec_id)
    join clean.companies c using (company_number)
    """).df()


def _decide(cands: pd.DataFrame) -> pd.DataFrame:
    if cands.empty:
        return pd.DataFrame(columns=["rec_id", "company_number", "match_method", "match_score", "confidence"])
    # token_sort_ratio forgives word order ('SMITH & SONS' vs 'SONS & SMITH') but punishes
    # typos inside short names; plain ratio is the reverse. Take the better of the two.
    cands["match_score"] = [
        max(fuzz.ratio(a, b), fuzz.token_sort_ratio(a, b)) if a and b else 0.0
        for a, b in zip(cands.rec_core, cands.cand_core)
    ]
    # Tie-breaker: similarity of the FULL normalised names, so 'X LIMITED' prefers
    # 'X LIMITED' over 'X HOLDINGS LIMITED' when both score well on the core.
    cands["full_score"] = [fuzz.ratio(a, b) for a, b in zip(cands.rec_full, cands.cand_full)]
    # exact_name is only trustworthy if that normalised name is unique among candidates
    n_exact = cands[cands.method == "exact_name"].groupby("rec_id").size()
    ambiguous = set(n_exact[n_exact > 1].index)

    def confidence(row):
        if row.method == "exact_name" and row.rec_id in ambiguous:
            return 0.0
        for method, min_score, conf in RULES:
            if row.method == method and row.match_score >= min_score:
                return conf
        return 0.0

    cands["confidence"] = cands.apply(confidence, axis=1)
    cands["is_active"] = (cands.status == "Active").astype(int)
    best = (cands[cands.confidence >= ACCEPT_AT]
            .sort_values(["rec_id", "confidence", "match_score", "full_score", "is_active"],
                         ascending=[True, False, False, False, False])
            .drop_duplicates("rec_id"))
    return best.rename(columns={"method": "match_method"})[
        ["rec_id", "company_number", "match_method", "match_score", "confidence"]]


def resolve(con: duckdb.DuckDBPyConnection, records_sql: str, out_table: str) -> None:
    best = _decide(_candidates(con, records_sql))
    best["needs_review"] = best.confidence < REVIEW_BELOW
    con.register("best_df", best)
    con.execute(f"create or replace table {out_table} as select * from best_df")


def resolve_all(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("create schema if not exists resolution")
    resolve(con, """select trader_id as rec_id, name_norm, name_core, postcode_norm,
                           null::varchar as stated_company_number
                    from clean.hmrc_trader
                    where trader_id in (select trader_id from clean.hmrc_trader_commodity)""",
            "resolution.trader_company")
    resolve(con, """select vendor_id as rec_id, name_norm, name_core, postcode_norm, stated_company_number
                    from clean.erp_supplier""",
            "resolution.supplier_company")


def evaluate_supplier_resolution(con: duckdb.DuckDBPyConnection, truth_csv) -> dict:
    """Precision/recall of vendor resolution against the synthetic ground truth."""
    t = pd.read_csv(truth_csv, dtype=str)
    p = con.execute("select rec_id as vendor_id, company_number from resolution.supplier_company").df()
    m = t.merge(p, on="vendor_id", how="left")
    predicted = m.company_number.notna()
    has_truth = m.true_company_number.notna()
    correct = predicted & (m.company_number == m.true_company_number)
    return {
        "vendors": len(m),
        "predicted_matches": int(predicted.sum()),
        "precision": round(correct.sum() / max(predicted.sum(), 1), 3),
        "recall": round(correct.sum() / max(has_truth.sum(), 1), 3),
        "false_matches_on_overseas": int((predicted & ~has_truth).sum()),
    }
