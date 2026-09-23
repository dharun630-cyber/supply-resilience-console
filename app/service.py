"""Read/act API for the dashboard. The UI only talks to this class, never to tables.

Reads come from the ontology (SQL for tabular lookups, the graph for traversal).
Writes go only through scm.actions, which validate and log. This is the same split
Foundry apps have between Object queries and Actions.
"""
from __future__ import annotations

import sys
import threading
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import duckdb  # noqa: E402
import pandas as pd  # noqa: E402

from scm import actions  # noqa: E402
from scm.config import RAW, WAREHOUSE  # noqa: E402
from scm.graph import (build_graph, event_impact, exposure_breakdown, nid,  # noqa: E402
                       refresh_link_type, refresh_object_type)
from scm.ingest.common import register_macros  # noqa: E402
from scm.ontology import load_ontology  # noqa: E402
from scm.transform.build import ensure_writeback  # noqa: E402
from scm.transform.scoring import priority  # noqa: E402

ACTOR = "resilience.lead"


class Console:
    def __init__(self):
        self.con = duckdb.connect(str(WAREHOUSE))
        register_macros(self.con)
        ensure_writeback(self.con)
        self.onto = load_ontology()
        self.G = build_graph(self.con, self.onto)
        self.lock = threading.RLock()
        self._impact = {}

    def q(self, sql: str, params=None) -> pd.DataFrame:
        with self.lock:
            return self.con.execute(sql, params or []).df()

    # ---- reads ---------------------------------------------------------------
    def freshness(self) -> dict:
        p = self.q("select min(period_start) s, max(period_end) e from ontology.import_flow").iloc[0]
        fmt = lambda m: pd.Timestamp(year=int(m) // 100, month=int(m) % 100, day=1).strftime("%b %Y")
        gd = sorted((RAW / "risk").glob("gdacs_*.json"))
        ch = sorted((RAW / "companies_house").glob("*.zip"))
        return {
            "trade": f"{fmt(p.s)} – {fmt(p.e)}",
            "events": pd.to_datetime(gd[-1].stem.split("_")[1]).strftime("%d %b %Y") if gd else "n/a",
            "companies": ch[-1].stem.split("-", 1)[1] if ch else "n/a",
        }

    def impact(self, event_id: str) -> pd.DataFrame:
        """Exposure depends only on source data, never on Actions, so it's cached per event."""
        if event_id not in self._impact:
            with self.lock:
                self._impact[event_id] = event_impact(self.G, event_id)
        return self._impact[event_id]

    def events(self, current_only: bool, threshold: float) -> pd.DataFrame:
        ev = self.q("""
            select e.*, string_agg(c.name, ', ' order by c.name) as countries, count(c.iso3) n_countries
            from ontology.risk_event e
            left join ontology.link_event_affects a using (event_id)
            left join ontology.country c using (iso3)
            group by all order by is_current desc, severity desc, start_date desc""")
        if current_only:
            ev = ev[ev.is_current]
        ev["material"] = [int((self.impact(e).get("exposure_score", pd.Series(dtype=float)) >= threshold).sum())
                          for e in ev.event_id]
        return ev

    def exposure_table(self, event_id: str, threshold: float) -> pd.DataFrame:
        imp = self.impact(event_id)
        if imp.empty:
            return imp
        cur = self.q("""
            select s.vendor_id, s.criticality, s.country_iso3, c.name as company_name, c.status as company_status,
                   c.fragility_score, c.fragility_reasons, r.needs_review,
                   a.alert_id, a.status as alert_status
            from ontology.erp_supplier s
            left join ontology.link_supplier_resolves_to r using (vendor_id)
            left join ontology.company c using (company_number)
            left join (select * from writeback.alert where event_id = ?
                       qualify row_number() over (partition by vendor_id order by created_at desc) = 1) a
                   using (vendor_id)""", [event_id])
        df = imp.drop(columns=["criticality"]).merge(cur, on="vendor_id", how="left")
        df["priority"] = [priority(e, c, f) for e, c, f in zip(df.exposure_score, df.criticality, df.fragility_score)]
        com = self.q("select cn8_code, hs4_description from ontology.commodity")
        df = df.merge(com.rename(columns={"cn8_code": "driver_commodity"}), on="driver_commodity", how="left")
        names = self.q("select iso3, name from ontology.country").set_index("iso3")["name"]
        df["driver_country_name"] = df.driver_country.map(names)
        return df.sort_values("priority", ascending=False)

    def event(self, event_id: str) -> dict:
        return self.q("""select e.*, string_agg(c.name, ', ' order by c.name) countries
                         from ontology.risk_event e left join ontology.link_event_affects using (event_id)
                         left join ontology.country c using (iso3) where event_id = ? group by all""",
                      [event_id]).iloc[0].to_dict()

    def supplier(self, vendor_id: str) -> dict:
        s = self.q("""
            select s.*, r.company_number, r.match_method, r.match_score, r.confidence, r.needs_review,
                   c.name company_name, c.status company_status, c.incorporated_on, c.sic_codes,
                   c.fragility_score, c.fragility_reasons,
                   (select count(*) from ontology.link_supplier_resolves_to r2
                     where r2.company_number = r.company_number and r2.vendor_id <> s.vendor_id) as duplicate_vendors
            from ontology.erp_supplier s
            left join ontology.link_supplier_resolves_to r using (vendor_id)
            left join ontology.company c using (company_number)
            where s.vendor_id = ?""", [vendor_id]).iloc[0].to_dict()
        s["alerts"] = self.q("""select a.*, e.name event_name from writeback.alert a
                                left join ontology.risk_event e using (event_id)
                                where vendor_id = ? order by created_at desc""", [vendor_id])
        return s

    def breakdown(self, event_id: str, vendor_id: str) -> list[dict]:
        with self.lock:
            return exposure_breakdown(self.G, event_id, vendor_id)

    def alerts(self) -> pd.DataFrame:
        return self.q("""select a.*, s.vendor_name, s.criticality, e.name event_name, e.alert_level
                         from writeback.alert a join ontology.erp_supplier s using (vendor_id)
                         left join ontology.risk_event e using (event_id)
                         order by (a.status = 'closed'), a.updated_at desc""")

    def health(self) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        return (self.q("select * from meta.validation"),
                self.q("select * from meta.data_quality"),
                self.q("select * from writeback.action_log order by logged_at desc limit 25"))

    def review_queue(self) -> pd.DataFrame:
        return self.q("""select s.vendor_id, s.vendor_name, s.postcode, s.stated_company_number,
                                r.company_number, c.name company_name, c.postcode company_postcode,
                                r.match_method, r.match_score, r.confidence
                         from ontology.link_supplier_resolves_to r
                         join ontology.erp_supplier s using (vendor_id)
                         left join ontology.company c using (company_number)
                         where r.needs_review order by r.confidence, s.vendor_name""")

    # ---- actions -------------------------------------------------------------
    def act(self, name: str, **kwargs) -> str | None:
        """Run an Action, then sync the graph objects it may have changed."""
        fn = getattr(actions, name)
        with self.lock:
            result = fn(self.con, actor=ACTOR, **kwargs)
            if name in {"confirm_match", "reject_match", "set_criticality"}:
                refresh_object_type(self.G, self.con, self.onto, "ERPSupplier")
                refresh_object_type(self.G, self.con, self.onto, "Company")
                refresh_link_type(self.G, self.con, self.onto, "resolves_to")
        return result


@lru_cache(maxsize=1)
def console() -> Console:
    return Console()
