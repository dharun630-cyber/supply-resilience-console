"""Clean layer -> ontology layer: one table per object type and per link type.

Writeback tables (user Actions) are created if missing and NEVER dropped. When the
pipeline rebuilds from source, user edits are re-applied on top (Foundry's
'edits survive re-indexing' behaviour).
"""
from __future__ import annotations

import duckdb
import pandas as pd
import pycountry

from ..config import HS_CHAPTERS
from .scoring import COMPANY_FRAGILITY_SQL, SANCTIONS_RISK

EXTRA_COUNTRY_NAMES = {"XKX": "Kosovo"}


def ensure_writeback(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("create schema if not exists writeback")
    con.execute("""create table if not exists writeback.alert (
        alert_id varchar primary key, vendor_id varchar, event_id varchar, status varchar,
        owner varchar, note varchar, created_at timestamp, updated_at timestamp)""")
    con.execute("""create table if not exists writeback.match_override (
        vendor_id varchar primary key, company_number varchar, decision varchar,
        decided_by varchar, decided_at timestamp)""")
    con.execute("""create table if not exists writeback.criticality_override (
        vendor_id varchar primary key, criticality varchar, decided_by varchar, decided_at timestamp)""")
    con.execute("""create table if not exists writeback.action_log (
        action varchar, params json, actor varchar, logged_at timestamp)""")


def build(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("create schema if not exists ontology")
    ensure_writeback(con)

    # ---- Commodity --------------------------------------------------------
    con.execute("""create or replace table ontology.commodity as
                   select cn8_code, description, hs2_code, hs4_code, hs4_description from clean.commodity""")

    # ---- ERPSupplier + PurchaseOrderLine ---------------------------------
    con.execute("""
    create or replace table ontology.po_line as
    select po_line_id, vendor_id, cn8_code, item_description, amount_gbp, order_date
    from clean.po_line where cn8_code in (select cn8_code from ontology.commodity)
    """)
    # ---- Company imports (from machine resolution of HMRC traders) ---------
    con.execute("""
    create or replace table ontology.link_company_imports as
    select distinct r.company_number, tc.cn8_code
    from resolution.trader_company r
    join clean.hmrc_trader_commodity tc on tc.trader_id = r.rec_id
    where tc.cn8_code in (select cn8_code from ontology.commodity)
    """)
    # ---- Everything user Actions can change --------------------------------
    apply_edits(con)

    # ---- RiskEvent --------------------------------------------------------
    con.execute("create or replace table ontology.risk_event as select * from clean.risk_event")

    # ---- Country ----------------------------------------------------------
    iso3s = [r[0] for r in con.execute("""
        select iso3 from clean.ots_imports where iso3 is not null
        union select iso3 from clean.event_country
        union select iso3 from clean.sanctions_by_country
        union select country_iso3 from ontology.erp_supplier where country_iso3 is not null
    """).fetchall()]
    names = []
    for i in iso3s:
        c = pycountry.countries.get(alpha_3=i)
        names.append((i, c.name if c else EXTRA_COUNTRY_NAMES.get(i, i)))
    con.register("cn_df", pd.DataFrame(names, columns=["iso3", "name"]))
    con.execute(f"""
    create or replace table ontology.country as
    with ev as (
        select a.iso3, max(e.severity) as sev
        from clean.event_country a join clean.risk_event e using (event_id)
        where e.is_current group by 1
    )
    select c.iso3, c.name,
           s.iso3 is not null as uk_sanctions_regime,
           coalesce(s.designations, 0) as sanctions_designations,
           coalesce(ev.sev, 0.0) as active_event_severity,
           greatest(coalesce(ev.sev, 0.0), case when s.iso3 is not null then {SANCTIONS_RISK} else 0 end) as country_risk
    from cn_df c
    left join clean.sanctions_by_country s using (iso3)
    left join ev using (iso3)
    """)
    con.execute("""create or replace table ontology.link_event_affects as
                   select distinct event_id, iso3 from clean.event_country""")

    # ---- ImportFlow -------------------------------------------------------
    # Share denominator includes flows we can't map to a country (confidential,
    # estimates, stores). Dropping them first would inflate every share.
    con.execute("""
    create or replace table ontology.import_flow as
    with totals as (select cn8_code, sum(value_gbp) tot from clean.ots_imports group by 1),
    by_country as (
        select cn8_code, iso3, any_value(period_start) period_start, any_value(period_end) period_end,
               sum(value_gbp) value_gbp
        from clean.ots_imports where iso3 is not null group by 1, 2
    )
    select cn8_code || '-' || iso3 || '-' || period_end as flow_id, b.cn8_code, b.iso3,
           b.period_start, b.period_end, b.value_gbp,
           b.value_gbp / nullif(t.tot, 0) as share_of_uk_imports
    from by_country b join totals t using (cn8_code)
    where b.cn8_code in (select cn8_code from ontology.commodity) and b.value_gbp > 0
    """)


def apply_edits(con: duckdb.DuckDBPyConnection) -> None:
    """(Re)build the ontology tables that user Actions can change: ERPSupplier (criticality),
    resolves_to (match overrides) and Company (a confirmed match can pull in a new company).
    Cheap (~1s), so Actions call it directly instead of re-running the whole pipeline.
    """
    iso2_to_3 = pd.DataFrame([(c.alpha_2, c.alpha_3) for c in pycountry.countries], columns=["a2", "a3"])
    con.register("iso_df", iso2_to_3)
    con.execute("""
    create or replace table ontology.erp_supplier as
    with spend as (select vendor_id, sum(amount_gbp) s from ontology.po_line group by 1)
    select s.vendor_id, s.vendor_name, s.postcode, s.country, i.a3 as country_iso3, s.stated_company_number,
           coalesce(sp.s, 0) as spend_12m_gbp,
           coalesce(o.criticality,
                    case when percent_rank() over (order by coalesce(sp.s, 0)) >= 0.8 then 'high'
                         when percent_rank() over (order by coalesce(sp.s, 0)) >= 0.4 then 'medium'
                         else 'low' end) as criticality
    from clean.erp_supplier s
    left join iso_df i on i.a2 = s.country
    left join spend sp using (vendor_id)
    left join writeback.criticality_override o using (vendor_id)
    """)

    # ---- resolves_to (machine match, then human overrides win) ------------
    con.execute("""
    create or replace table ontology.link_supplier_resolves_to as
    with machine as (
        select rec_id as vendor_id, company_number, match_method, match_score, confidence, needs_review
        from resolution.supplier_company
    )
    select vendor_id,
           case when o.decision = 'reject' then null else coalesce(o.company_number, m.company_number) end as company_number,
           case when o.decision is not null then 'human_' || o.decision else m.match_method end as match_method,
           m.match_score,
           case when o.decision = 'confirm' then 1.0 when o.decision = 'reject' then null else m.confidence end as confidence,
           coalesce(o.decision is null and m.needs_review, false) as needs_review
    from machine m full outer join writeback.match_override o using (vendor_id)
    where coalesce(o.decision, '') <> 'reject'
    """)

    # ---- Company: only those the use case touches ------------------------
    con.execute(f"""
    create or replace table ontology.company as
    with base as (
        select company_number, name, status, category, postcode, incorporated_on, sic_codes,
               mortgages_outstanding,
               coalesce(accounts_next_due < current_date, false) as accounts_overdue,
               coalesce(confstmt_next_due < current_date, false) as confirmation_overdue
        from clean.companies
        where company_number in (select company_number from ontology.link_company_imports
                                 union select company_number from ontology.link_supplier_resolves_to)
    )
    {COMPANY_FRAGILITY_SQL}
    """)


def data_quality_report(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """Source-level metrics: what the pipeline had to drop or couldn't resolve, and why."""
    q = lambda sql: con.execute(sql).fetchone()[0]
    chapters = ", ".join(f"'{c}'" for c in HS_CHAPTERS)
    return [
        {"metric": "HMRC in-scope traders", "value": q("select count(distinct trader_id) from clean.hmrc_trader_commodity")},
        {"metric": "  resolved to a Company", "value": q("select count(*) from resolution.trader_company")},
        {"metric": "  of which needing review", "value": q("select count(*) from resolution.trader_company where needs_review")},
        {"metric": "Trader CN8 codes not in current nomenclature (dropped)",
         "value": q("select count(distinct cn8_code) from clean.hmrc_trader_commodity where cn8_code not in (select cn8_code from clean.commodity)")},
        {"metric": "OTS value with no mappable country (GBP, kept in share denominator)",
         "value": round(q("select coalesce(sum(value_gbp),0) from clean.ots_imports where iso3 is null"))},
        {"metric": "ERP vendors", "value": q("select count(*) from clean.erp_supplier")},
        {"metric": "  resolved to a Company", "value": q("select count(company_number) from ontology.link_supplier_resolves_to")},
        {"metric": "  needing human review", "value": q("select count(*) from ontology.link_supplier_resolves_to where needs_review")},
        {"metric": "  ERP vendors sharing a Company (probable duplicates)",
         "value": q("select coalesce(sum(n),0) from (select count(*) n from ontology.link_supplier_resolves_to where company_number is not null group by company_number having count(*) > 1)")},
        {"metric": "PO lines in non-GBP currency (converted)", "value": q("select count(*) from clean.po_line where original_currency <> 'GBP'")},
        {"metric": "Current Orange/Red risk events", "value": q("select count(*) from clean.risk_event where is_current")},
    ]
