"""HMRC uktradeinfo: (i) monthly importer list, (ii) Overseas Trade Statistics via OData API.

Quirks handled here:
- Importer file: tab-separated, no header, no company number. 9 fixed fields then up to
  50 CN8 codes; traders with more codes spill onto continuation rows (field 2 = 2, 3, ...).
- OTS: split by port, so rows are aggregated server-side with OData $apply.
- Country codes are HMRC's own: 'XS' = Serbia, Dubai/Abu Dhabi/Sharjah are separate
  'countries', and there are non-geographic codes (stores, estimates, confidential).
"""
from __future__ import annotations

import json
import zipfile

import duckdb
import pandas as pd
import pycountry

from ..config import HMRC_API, HMRC_BULK_INDEX, HS_CHAPTERS, OTS_END_MONTH
from .common import SESSION, download, latest_link, raw_dir

# HMRC alpha-2 codes that are not ISO 3166, mapped to the ISO3 country they belong to.
HMRC_COUNTRY_OVERRIDES = {
    "XS": "SRB", "XK": "XKX", "XC": "ESP", "XL": "ESP",
    "DH": "ARE", "DU": "ARE", "HA": "ARE",
}


def fetch_importers():
    link = latest_link(HMRC_BULK_INDEX, r'/media/[a-z0-9]+/importers\d{4}\.zip')
    return download("https://www.uktradeinfo.com" + link, raw_dir("hmrc") / link.split("/")[-1])


def _get_all(url: str) -> list[dict]:
    rows = []
    while url:
        d = SESSION.get(url, timeout=120).json()
        if "error" in d:
            raise RuntimeError(d["error"])
        rows += d["value"]
        url = d.get("@odata.nextLink")
    return rows


def latest_ots_month() -> int:
    d = SESSION.get(f"{HMRC_API}/OTS?$apply=aggregate(MonthId with max as m)", timeout=60).json()
    return d["value"][0]["m"]


def fetch_ots():
    """Trailing 12 months of imports (EU + non-EU flows), summed over ports, per CN8 x country."""
    end = OTS_END_MONTH or latest_ots_month()
    y, m = divmod(end, 100)
    start = (y - 1) * 100 + m + 1 if m < 12 else y * 100 + 1
    # Scope is part of the cache key, so changing HS_CHAPTERS never reuses stale files
    out = raw_dir("hmrc") / f"ots_{'-'.join(HS_CHAPTERS)}_{start}_{end}.json"
    if not out.exists():
        rows = []
        for ch in HS_CHAPTERS:
            lo, hi = int(ch) * 1_000_000, (int(ch) + 1) * 1_000_000
            url = (f"{HMRC_API}/OTS?$apply=filter(MonthId ge {start} and MonthId le {end}"
                   f" and (FlowTypeId eq 1 or FlowTypeId eq 3)"
                   f" and CommodityId ge {lo} and CommodityId lt {hi})"
                   f"/groupby((CommodityId,CountryId),aggregate(Value with sum as Value))")
            rows += _get_all(url)
        json.dump({"start": start, "end": end, "rows": rows}, open(out, "w"))
    return out


def fetch_reference():
    d = raw_dir("hmrc")
    cc = d / f"commodity_{'-'.join(HS_CHAPTERS)}.json"
    if not cc.exists():
        flt = " or ".join(f"Hs2Code eq '{c}'" for c in HS_CHAPTERS)
        json.dump(_get_all(f"{HMRC_API}/Commodity?$filter={flt}"), open(cc, "w"))
    co = d / "country.json"
    if not co.exists():
        json.dump(_get_all(f"{HMRC_API}/Country"), open(co, "w"))
    return cc, co


def _alpha2_to_iso3(a2: str | None) -> str | None:
    if not a2:
        return None
    if a2 in HMRC_COUNTRY_OVERRIDES:
        return HMRC_COUNTRY_OVERRIDES[a2]
    c = pycountry.countries.get(alpha_2=a2)
    return c.alpha_3 if c else None


def load_clean(con: duckdb.DuckDBPyConnection, importers_zip, ots_json, commodity_json, country_json):
    # --- importers --------------------------------------------------------
    with zipfile.ZipFile(importers_zip) as z:
        txt = z.read(z.namelist()[0]).decode("latin-1")
    recs, codes = {}, []
    for line in txt.splitlines():
        f = line.split("\t")
        if len(f) < 10 or not f[0].isdigit():
            continue
        name, postcode = f[2].strip(), f[8].strip()
        key = (name, postcode)
        if key not in recs:  # first (seq 1) row carries the address
            recs[key] = {"month_id": int(f[0]), "name": name, "postcode": postcode,
                         "address": ", ".join(x.strip() for x in f[3:8] if x.strip())}
        codes += [(name, postcode, c.strip()) for c in f[9:] if c.strip()]
    traders = pd.DataFrame(recs.values())
    tcodes = pd.DataFrame(codes, columns=["name", "postcode", "cn8_code"]).drop_duplicates()
    con.register("traders_df", traders)
    con.register("tcodes_df", tcodes)
    con.execute("""
    create or replace table clean.hmrc_trader as
    select md5(name || '|' || postcode) as trader_id, month_id, name, address, postcode,
           norm_name(name) as name_norm, name_core(name) as name_core,
           norm_postcode(postcode) as postcode_norm
    from traders_df
    """)
    chapters = ", ".join(f"'{c}'" for c in HS_CHAPTERS)
    con.execute(f"""
    create or replace table clean.hmrc_trader_commodity as
    select distinct md5(name || '|' || postcode) as trader_id, cn8_code
    from tcodes_df where left(cn8_code, 2) in ({chapters})
    """)

    # --- reference data ---------------------------------------------------
    com = pd.DataFrame(json.load(open(commodity_json)))
    com = com[com["Cn8Code"].str.len() == 8]
    con.register("com_df", com)
    con.execute("""
    create or replace table clean.commodity as
    select Cn8Code as cn8_code, Cn8LongDescription as description, Hs2Code as hs2_code,
           Hs4Code as hs4_code, Hs4Description as hs4_description, Hs6Description as hs6_description
    from com_df
    """)
    cty = pd.DataFrame(json.load(open(country_json)))
    cty["iso3"] = cty["CountryCodeAlpha"].map(_alpha2_to_iso3)
    con.register("cty_df", cty)
    con.execute("""
    create or replace table clean.hmrc_country as
    select CountryId as hmrc_country_id, CountryCodeAlpha as hmrc_alpha, trim(CountryName) as hmrc_name, iso3
    from cty_df
    """)

    # --- OTS --------------------------------------------------------------
    ots = json.load(open(ots_json))
    df = pd.DataFrame(ots["rows"]).drop(columns=["@odata.id"], errors="ignore")
    df["period_start"], df["period_end"] = ots["start"], ots["end"]
    con.register("ots_df", df)
    con.execute("""
    create or replace table clean.ots_imports as
    select lpad(CommodityId::varchar, 8, '0') as cn8_code, c.iso3, c.hmrc_name,
           o.CountryId as hmrc_country_id, o.Value as value_gbp, period_start, period_end
    from ots_df o left join clean.hmrc_country c on c.hmrc_country_id = o.CountryId
    """)
