"""Shared helpers for the ingest layer: cached downloads and the name/postcode normalisation macros."""
from __future__ import annotations

import re
from pathlib import Path

import duckdb
import requests

from ..config import RAW

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "scm-ontology-portfolio/0.1"


def download(url: str, dest: Path, force: bool = False) -> Path:
    """Download once; raw files are immutable inputs, so re-runs reuse them."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not force:
        return dest
    with SESSION.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".part")
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
        tmp.rename(dest)
    return dest


def latest_link(index_url: str, pattern: str) -> str:
    """Find the newest file link on a publisher's index page (file names embed dates)."""
    html = SESSION.get(index_url, timeout=60).text
    links = sorted(set(re.findall(pattern, html)))
    if not links:
        raise RuntimeError(f"no link matching {pattern!r} on {index_url}")
    return links[-1]


def register_macros(con: duckdb.DuckDBPyConnection) -> None:
    """SQL macros so every source is normalised by exactly the same rules.

    norm_name:     'The Acme (U.K.) Ltd.'  -> 'ACME UK LIMITED'
    norm_postcode: 'wa14 2dt ' -> 'WA142DT'
    """
    con.execute(r"""
    create or replace macro norm_name(s) as trim(regexp_replace(
        regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(
            upper(coalesce(s, '')),
            '&', ' AND ', 'g'),
            '[^A-Z0-9 ]', ' ', 'g'),                         -- drop punctuation
            '\bLTD\b', 'LIMITED', 'g'),
            '\b(CO)\b', 'COMPANY', 'g'),
            '^THE ', '', 'g'),
        '\s+', ' ', 'g'));
    """)
    con.execute(r"""
    create or replace macro norm_postcode(p) as
        nullif(regexp_replace(upper(coalesce(p, '')), '[^A-Z0-9]', '', 'g'), '');
    """)
    # Name with the LEGAL FORM removed, used for similarity scoring.
    # Only strip words that never distinguish two companies. GROUP / HOLDINGS / UK DO
    # distinguish them ('X LIMITED' vs 'X HOLDINGS LIMITED' is often parent vs subsidiary
    # at the same address), so they stay.
    # The trailing pattern also catches typo'd suffixes: LIMTED, LMITED, LIMITTED.
    con.execute(r"""
    create or replace macro name_core(s) as trim(regexp_replace(regexp_replace(norm_name(s),
        '\b(LIMITED|PLC|LLP|LP|INCORPORATED|INC|CORPORATION|CORP|GMBH)\b', '', 'g'),
        '\bL[IMTED]{4,7}$', ''));
    """)


def raw_dir(source: str) -> Path:
    d = RAW / source
    d.mkdir(parents=True, exist_ok=True)
    return d
