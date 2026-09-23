"""Risk signals: GDACS disaster alerts and the UK Sanctions List."""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re

import duckdb
import pandas as pd
import pycountry

from ..config import GDACS_API, SANCTIONS_CSV
from .common import SESSION, download, raw_dir

ALERT_SEVERITY = {"Red": 1.0, "Orange": 0.6, "Green": 0.2}


def fetch_gdacs(days_back: int = 120):
    today = dt.date.today()
    out = raw_dir("risk") / f"gdacs_{today:%Y%m%d}.json"
    if not out.exists():
        params = {"eventlist": "EQ;TC;FL;DR;VO;WF", "alertlevel": "Orange;Red",
                  "fromdate": f"{today - dt.timedelta(days=days_back)}", "todate": f"{today}"}
        r = SESSION.get(GDACS_API, params=params, timeout=120)
        r.raise_for_status()
        out.write_text(r.text)
    return out


def fetch_sanctions():
    return download(SANCTIONS_CSV, raw_dir("risk") / f"uk_sanctions_{dt.date.today():%Y%m%d}.csv")


REGIME_COUNTRY_OVERRIDES = {
    "Democratic People's Republic of Korea": "PRK",
    "Republic of Belarus": "BLR",
    "Republic of Guinea-Bissau": "GNB",
    "Democratic Republic of the Congo": "COD",
    "Russia": "RUS", "Iran": "IRN", "Syria": "SYR", "Venezuela": "VEN",
}


def _regime_to_iso3(regime: str) -> str | None:
    """'The Russia (Sanctions) (EU Exit) Regulations 2019' -> 'RUS'. Thematic regimes -> None."""
    m = re.match(r"^(?:The )?(.+?) \(Sanctions\)", regime)
    if not m:
        return None  # e.g. 'The Global Human Rights Sanctions Regulations 2020'
    subject = m.group(1)
    if subject in REGIME_COUNTRY_OVERRIDES:
        return REGIME_COUNTRY_OVERRIDES[subject]
    try:
        return pycountry.countries.lookup(subject).alpha_3
    except LookupError:
        return None  # Counter-Terrorism, Cyber, Chemical Weapons, ...


def load_clean(con: duckdb.DuckDBPyConnection, gdacs_json, sanctions_csv):
    feats = json.load(open(gdacs_json)).get("features", [])
    events, affects = [], []
    for f in feats:
        p = f["properties"]
        eid = f"GDACS-{p['eventtype']}-{p['eventid']}"
        events.append({
            "event_id": eid, "source": "GDACS", "event_type": p["eventtype"],
            "name": p.get("name") or p.get("description"),
            "alert_level": p["alertlevel"], "severity": ALERT_SEVERITY.get(p["alertlevel"], 0.0),
            "start_date": p["fromdate"][:10], "end_date": p["todate"][:10],
            "is_current": str(p.get("iscurrent")).lower() == "true",
            "url": p.get("url", {}).get("report"),
        })
        for c in p.get("affectedcountries") or [{"iso3": p.get("iso3")}]:
            if c.get("iso3"):
                affects.append({"event_id": eid, "iso3": c["iso3"]})
    con.register("ev_df", pd.DataFrame(events).drop_duplicates("event_id"))
    con.register("af_df", pd.DataFrame(affects).drop_duplicates())
    con.execute("""create or replace table clean.risk_event as
                   select * replace (start_date::date as start_date, end_date::date as end_date) from ev_df""")
    con.execute("create or replace table clean.event_country as select * from af_df")

    # Sanctions: first line is 'Report Date: ...', header is on line 2.
    text = open(sanctions_csv, encoding="utf-8-sig").read().split("\n", 1)[1]
    rows = list(csv.DictReader(io.StringIO(text)))
    df = pd.DataFrame({"unique_id": [r["Unique ID"] for r in rows],
                       "regime": [r["Regime Name"] for r in rows],
                       "designation_type": [r["Designation Type"] for r in rows]})
    df["iso3"] = df["regime"].map(_regime_to_iso3)
    con.register("sanc_df", df)
    # One designation appears on many rows (one per alias): count distinct IDs.
    con.execute("""
    create or replace table clean.sanctions_by_country as
    select iso3, count(distinct unique_id) as designations, string_agg(distinct regime, '; ') as regimes
    from sanc_df where iso3 is not null group by iso3
    """)
