"""The client's ERP extract: a synthetic Oracle-style AP_SUPPLIERS + PO_LINES export.

generate() fabricates it from REAL importers, then corrupts it the way real vendor
masters are corrupted (I saw every one of these at EY):
  - LTD/LIMITED drift, dropped '(UK)', typos, mixed case
  - registration numbers missing, or keyed without leading zeros (Excel ate them)
  - the same supplier set up twice under different vendor IDs
  - overseas suppliers that have no Companies House record at all
  - PO lines in GBP/EUR/USD, Oracle DD-MON-YY dates
It also writes a ground-truth file that the pipeline never reads, used only by
evaluate_resolution() to measure entity-resolution precision and recall.
"""
from __future__ import annotations

import random

import duckdb
import numpy as np
import pandas as pd

from ..config import HS_CHAPTERS, RANDOM_SEED
from .common import raw_dir

FX_TO_GBP = {"GBP": 1.0, "EUR": 0.85, "USD": 0.78}  # fixed for reproducibility; would be a rates table in reality

OVERSEAS = [
    ("SHENZHEN HUAXING ELECTRONIC COMPONENTS CO LTD", "CN", "8542"),
    ("TAIWAN PRECISION SEMICONDUCTOR CORP", "TW", "8541"),
    ("JIANGSU YONGGANG STEEL WIRE CO", "CN", "7217"),
    ("NINGBO ORIENT BEARING CO LTD", "CN", "8482"),
    ("SIEMENS AG", "DE", "8504"),
    ("PT KRAKATAU STEEL", "ID", "7208"),
    ("TATA STEEL LIMITED", "IN", "7210"),
    ("BASF SE", "DE", "2915"),
    ("SAMSUNG ELECTRO-MECHANICS", "KR", "8532"),
    ("MURATA MANUFACTURING CO LTD", "JP", "8532"),
    ("HAI PHONG CABLE JSC", "VN", "8544"),
    ("TURKISH DRIVE SYSTEMS AS", "TR", "8483"),
]


def _typo(rng: random.Random, s: str) -> str:
    i = rng.randrange(1, max(2, len(s) - 1))
    op = rng.choice(["swap", "drop", "dup"])
    if op == "swap" and i < len(s) - 1:
        return s[:i] + s[i + 1] + s[i] + s[i + 2:]
    if op == "drop":
        return s[:i] + s[i + 1:]
    return s[:i] + s[i] + s[i:]


def _corrupt_name(rng: random.Random, name: str) -> str:
    n = name
    if rng.random() < 0.5:
        n = n.replace("LIMITED", "LTD") if "LIMITED" in n else n.replace("LTD", "LIMITED")
    if rng.random() < 0.3:
        n = n.replace(" (UK)", "").replace(" UK ", " ")
    if rng.random() < 0.2:
        n = _typo(rng, n)
    if rng.random() < 0.4:
        n = n.title()
    if rng.random() < 0.15:
        n = n.replace(" AND ", " & ")
    return n


def _oracle_date(d) -> str:
    return pd.Timestamp(d).strftime("%d-%b-%y").upper()  # 14-MAR-26


def generate(con: duckdb.DuckDBPyConnection, n_suppliers: int = 180) -> None:
    rng = random.Random(RANDOM_SEED)
    nprng = np.random.default_rng(RANDOM_SEED)
    chapters = ", ".join(f"'{c}'" for c in HS_CHAPTERS)

    # Real in-scope importers that map unambiguously to one ACTIVE company (our ground truth).
    pool = con.execute(f"""
    with t as (
        select t.trader_id, t.name, t.address, t.postcode, t.name_norm, t.postcode_norm
        from clean.hmrc_trader t
        where t.trader_id in (select trader_id from clean.hmrc_trader_commodity)
    ), m as (
        select t.*, c.company_number, c.name as ch_name
        from t join clean.companies c
          on c.name_norm = t.name_norm and c.postcode_norm = t.postcode_norm
        where c.status = 'Active'
    )
    select * from m qualify count(*) over (partition by trader_id) = 1
    order by trader_id
    """).df()
    pool = pool.sample(n=n_suppliers, random_state=RANDOM_SEED).reset_index(drop=True)
    tcodes = con.execute(f"""select trader_id, cn8_code from clean.hmrc_trader_commodity
                             where cn8_code in (select cn8_code from clean.commodity)""").df()
    codes_by_trader = tcodes.groupby("trader_id")["cn8_code"].apply(list).to_dict()
    all_codes = con.execute("select cn8_code, description, hs4_code from clean.commodity").df()

    suppliers, truth, lines = [], [], []
    vid = 10_000

    def add_vendor(name, postcode, address, country, reg_no, true_company, cn8_choices):
        nonlocal vid
        vid += rng.randint(1, 40)
        v = f"V{vid}"
        suppliers.append({
            "VENDOR_ID": v, "VENDOR_NAME": name, "SEGMENT1": f"SUP-{vid}",
            "ADDRESS_LINE1": address, "ZIP": postcode, "COUNTRY": country,
            "REGISTRATION_NUMBER": reg_no,
            "CREATION_DATE": _oracle_date(pd.Timestamp("2015-01-01") + pd.Timedelta(days=rng.randint(0, 3800))),
            "ENABLED_FLAG": "Y",
        })
        truth.append({"vendor_id": v, "true_company_number": true_company})
        # 12 months of PO lines, lognormal spend, 1-3 commodities per vendor
        for cn8 in rng.sample(cn8_choices, k=min(len(cn8_choices), rng.randint(1, 3))):
            desc = all_codes.loc[all_codes.cn8_code == cn8, "description"]
            for _ in range(rng.randint(2, 14)):
                cur = rng.choices(["GBP", "EUR", "USD"], [0.75, 0.15, 0.10])[0] if country == "GB" \
                    else rng.choice(["EUR", "USD"])
                lines.append({
                    "PO_NUMBER": f"PO{rng.randint(100000, 999999)}", "LINE_NUM": 1, "VENDOR_ID": v,
                    "CATEGORY_CODE": cn8,
                    "ITEM_DESCRIPTION": (desc.iloc[0][:60] if len(desc) else "ITEM"),
                    "QUANTITY": int(nprng.integers(1, 500)),
                    "UNIT_PRICE": round(float(nprng.lognormal(3.5, 1.2)), 2),
                    "CURRENCY_CODE": cur,
                    "CREATION_DATE": _oracle_date(pd.Timestamp.today() - pd.Timedelta(days=rng.randint(1, 365))),
                })
        return v

    for _, r in pool.iterrows():
        codes = [c for c in codes_by_trader.get(r.trader_id, []) if c in set(all_codes.cn8_code)]
        if not codes:
            continue
        roll = rng.random()
        if roll < 0.55:
            reg = r.company_number                       # correct
        elif roll < 0.65:
            reg = r.company_number.lstrip("0")           # Excel stripped leading zeros
        elif roll < 0.70:
            reg = r.company_number[:-2] + r.company_number[-1] + r.company_number[-2]  # transposed
        else:
            reg = ""                                     # never captured
        pc = r.postcode if rng.random() < 0.8 else (r.postcode.replace(" ", "").lower() if rng.random() < 0.7 else "")
        name = _corrupt_name(rng, r["name"])
        add_vendor(name, pc, r.address.split(",")[0], "GB", reg, r.company_number, codes)
        if rng.random() < 0.08:  # duplicate vendor record for the same company
            add_vendor(_corrupt_name(rng, r["name"]), pc, r.address.split(",")[0], "GB", "",
                       r.company_number, codes)

    for name, country, hs4 in OVERSEAS:
        codes = all_codes.loc[all_codes.hs4_code == hs4, "cn8_code"].tolist() or all_codes.cn8_code.tolist()[:5]
        add_vendor(name, "", "", country, "", None, codes)

    d = raw_dir("erp")
    pd.DataFrame(suppliers).to_csv(d / "AP_SUPPLIERS.csv", index=False)
    pd.DataFrame(lines).to_csv(d / "PO_LINES.csv", index=False)
    pd.DataFrame(truth).to_csv(d / "_ground_truth_DO_NOT_USE_IN_PIPELINE.csv", index=False)


def load_clean(con: duckdb.DuckDBPyConnection) -> None:
    d = raw_dir("erp")
    con.execute(f"""
    create or replace table clean.erp_supplier as
    select VENDOR_ID as vendor_id, VENDOR_NAME as vendor_name,
           norm_name(VENDOR_NAME) as name_norm, name_core(VENDOR_NAME) as name_core,
           ZIP as postcode, norm_postcode(ZIP) as postcode_norm, COUNTRY as country,
           -- Registration numbers are 8 chars; restore leading zeros Excel stripped
           case when regexp_matches(REGISTRATION_NUMBER, '^[0-9]+$') then lpad(REGISTRATION_NUMBER, 8, '0')
                else nullif(upper(trim(REGISTRATION_NUMBER)), '') end as stated_company_number,
           strptime(CREATION_DATE, '%d-%b-%y')::date as created_on
    from read_csv('{d / "AP_SUPPLIERS.csv"}', all_varchar=true)
    """)
    fx = " ".join(f"when '{k}' then {v}" for k, v in FX_TO_GBP.items())
    con.execute(f"""
    create or replace table clean.po_line as
    select PO_NUMBER || '-' || row_number() over (partition by PO_NUMBER order by VENDOR_ID, CATEGORY_CODE) as po_line_id,
           VENDOR_ID as vendor_id, lpad(CATEGORY_CODE, 8, '0') as cn8_code, ITEM_DESCRIPTION as item_description,
           QUANTITY::double * UNIT_PRICE::double * (case CURRENCY_CODE {fx} end) as amount_gbp,
           CURRENCY_CODE as original_currency,
           strptime(CREATION_DATE, '%d-%b-%y')::date as order_date
    from read_csv('{d / "PO_LINES.csv"}', all_varchar=true)
    """)
