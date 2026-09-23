"""Companies House 'Basic Company Data' (~5.7M rows, 2.8 GB CSV).

Why not just read_csv? The published header has 4 SIC columns but some rows carry 5,
which shifts every later column. A positional loader silently mislabels those rows,
so we parse with Python's csv module and pick SIC codes out by *pattern*.
"""
from __future__ import annotations

import csv
import re
import zipfile

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from ..config import COMPANIES_HOUSE_INDEX
from .common import download, latest_link, raw_dir

SIC_RE = re.compile(r"^(\d{5}) - |^None Supplied$")
BASE = "https://download.companieshouse.gov.uk/"


def fetch() -> "Path":
    name = latest_link(COMPANIES_HOUSE_INDEX, r"BasicCompanyDataAsOneFile-\d{4}-\d{2}-\d{2}\.zip")
    return download(BASE + name, raw_dir("companies_house") / name)


def _parse_rows(csv_path):
    """Yield dicts. Fixed-position fields up to column 25; SIC codes by pattern; tail from the right."""
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader)
        for row in reader:
            if len(row) < 30:
                continue  # blank / truncated line
            sics, i = [], 26
            while i < len(row) and SIC_RE.match(row[i]):
                m = SIC_RE.match(row[i])
                if m.group(1):
                    sics.append(m.group(1))
                i += 1
            yield {
                "company_number": row[1].strip(),
                "name": row[0].strip(),
                "address_line1": row[4].strip(),
                "post_town": row[6].strip(),
                "postcode": row[9].strip(),
                "category": row[10],
                "status": row[11],
                "dissolved_on": row[13],
                "incorporated_on": row[14],
                "accounts_next_due": row[17],
                "accounts_category": row[19],
                "mortgages_outstanding": row[23],
                "sic_codes": sics,
                # Confirmation-statement dates are the last two columns: index from the right.
                "confstmt_next_due": row[-2],
            }


def to_parquet(zip_path) -> "Path":
    out = zip_path.with_suffix(".parquet")
    if out.exists():
        return out
    with zipfile.ZipFile(zip_path) as z:
        member = z.namelist()[0]
        csv_path = zip_path.parent / member
        if not csv_path.exists():
            z.extract(member, zip_path.parent)
    writer, batch = None, []
    for rec in _parse_rows(csv_path):
        batch.append(rec)
        if len(batch) == 250_000:
            tbl = pa.Table.from_pylist(batch)
            writer = writer or pq.ParquetWriter(out, tbl.schema)
            writer.write_table(tbl)
            batch = []
    if batch:
        tbl = pa.Table.from_pylist(batch)
        writer = writer or pq.ParquetWriter(out, tbl.schema)
        writer.write_table(tbl)
    writer.close()
    return out


def load_clean(con: duckdb.DuckDBPyConnection, parquet_path) -> None:
    con.execute(f"""
    create or replace table clean.companies as
    select
        company_number,
        name,
        norm_name(name)            as name_norm,
        name_core(name)            as name_core,
        norm_postcode(postcode)    as postcode_norm,
        postcode, address_line1, post_town,
        category, status,
        try_strptime(nullif(incorporated_on, ''), '%d/%m/%Y')::date   as incorporated_on,
        try_strptime(nullif(dissolved_on, ''), '%d/%m/%Y')::date      as dissolved_on,
        try_strptime(nullif(accounts_next_due, ''), '%d/%m/%Y')::date as accounts_next_due,
        try_strptime(nullif(confstmt_next_due, ''), '%d/%m/%Y')::date as confstmt_next_due,
        accounts_category,
        try_cast(mortgages_outstanding as integer) as mortgages_outstanding,
        sic_codes
    from read_parquet('{parquet_path}')
    """)
