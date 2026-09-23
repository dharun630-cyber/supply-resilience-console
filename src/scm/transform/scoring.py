"""Derived properties. Each score is a transparent sum of named components, so the
dashboard can always answer 'why is this number high?'.
"""
from __future__ import annotations

# Company fragility: signals from Companies House filings
FRAGILITY_WEIGHTS = {
    "not_active": 1.0,            # liquidation, administration, strike-off proposed...
    "accounts_overdue": 0.35,
    "confirmation_overdue": 0.20,
    "younger_than_2y": 0.15,
    "charges_outstanding": 0.10,  # secured lending against the company
}

# Country risk: the worst of any current disaster and a UK sanctions regime
SANCTIONS_RISK = 0.8

# Triage priority shown in the dashboard:
#   priority = exposure x criticality weight x (1 + fragility) / 2
# i.e. how hard the event hits us, scaled by how much we depend on the supplier; a
# financially fragile supplier counts up to double. Range 0-1.
CRITICALITY_WEIGHT = {"high": 1.0, "medium": 0.6, "low": 0.3}


def priority(exposure: float, criticality: str, fragility: float | None) -> float:
    return exposure * CRITICALITY_WEIGHT.get(criticality, 0.6) * (1 + (fragility or 0.0)) / 2

COMPANY_FRAGILITY_SQL = f"""
select *,
  least(1.0::double,
      {FRAGILITY_WEIGHTS['not_active']}           * (status <> 'Active')::int
    + {FRAGILITY_WEIGHTS['accounts_overdue']}     * accounts_overdue::int
    + {FRAGILITY_WEIGHTS['confirmation_overdue']} * confirmation_overdue::int
    + {FRAGILITY_WEIGHTS['younger_than_2y']}      * (incorporated_on > current_date - interval 2 year)::int
    + {FRAGILITY_WEIGHTS['charges_outstanding']}  * (coalesce(mortgages_outstanding, 0) > 0)::int
  ) as fragility_score,
  list_filter([
      case when status <> 'Active' then 'status: ' || status end,
      case when accounts_overdue then 'accounts overdue' end,
      case when confirmation_overdue then 'confirmation statement overdue' end,
      case when incorporated_on > current_date - interval 2 year then 'incorporated < 2 years ago' end,
      case when coalesce(mortgages_outstanding, 0) > 0 then 'outstanding charges' end
  ], x -> x is not null) as fragility_reasons
from base
"""


def supplier_exposure_sql(event_id: str | None = None) -> str:
    """Exposure of each ERP supplier, 0-1.

    For each commodity we buy from the supplier (weighted by our spend share):
      - UK supplier:       sum over countries of (UK import share from that country x country risk)
                           i.e. we inherit the UK's sourcing mix, an INFERRED path
      - overseas supplier: the risk of the supplier's own country, a DIRECT path

    event_id=None scores against all current risk; otherwise against one event only
    ('what if this earthquake is the only thing that happened?').

    This is the multi-hop traversal written as SQL; graph.py walks the same path in
    NetworkX, and tests check the two agree.
    """
    if event_id is None:
        country_risk = "select iso3, country_risk as risk from ontology.country"
    else:
        country_risk = f"""
        select a.iso3, e.severity as risk
        from ontology.link_event_affects a join ontology.risk_event e using (event_id)
        where event_id = '{event_id}'"""
    return f"""
    with cr as ({country_risk}),
    spend as (
        select p.vendor_id, p.cn8_code, s.country_iso3, sum(p.amount_gbp) as spend
        from ontology.po_line p join ontology.erp_supplier s using (vendor_id)
        group by all
    ), weights as (
        select *, spend / sum(spend) over (partition by vendor_id) as w from spend
    ), inferred as (
        select f.cn8_code, sum(f.share_of_uk_imports * cr.risk) as risk,
               arg_max(f.iso3, f.share_of_uk_imports * cr.risk) as top_country
        from ontology.import_flow f join cr using (iso3)
        group by 1
    ), per_line as (
        select w.vendor_id, w.cn8_code, w.w,
               case when w.country_iso3 = 'GBR' then coalesce(i.risk, 0) else coalesce(d.risk, 0) end as risk,
               case when w.country_iso3 = 'GBR' then i.top_country else w.country_iso3 end as via_country,
               case when w.country_iso3 = 'GBR' then 'inferred' else 'direct' end as path_type
        from weights w
        left join inferred i using (cn8_code)
        left join cr d on d.iso3 = w.country_iso3
    )
    select vendor_id,
           least(1.0, sum(w * risk))            as exposure_score,
           arg_max(cn8_code, w * risk)          as driver_commodity,
           arg_max(via_country, w * risk)       as driver_country,
           any_value(path_type)                 as path_type
    from per_line group by 1
    """
