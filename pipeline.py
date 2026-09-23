"""Run the full build: raw -> clean -> resolution -> ontology -> validation.

    python pipeline.py            # full run (downloads are cached in data/raw)
    python pipeline.py --regen-erp  # regenerate the synthetic ERP extract
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

import duckdb  # noqa: E402
import pandas as pd  # noqa: E402

from scm.config import RAW, WAREHOUSE  # noqa: E402
from scm.ingest import companies_house, erp, hmrc, risk  # noqa: E402
from scm.ingest.common import register_macros  # noqa: E402
from scm.ontology import load_ontology, validate  # noqa: E402
from scm.transform import build as build_mod  # noqa: E402
from scm.transform.resolve import evaluate_supplier_resolution, resolve_all  # noqa: E402


def step(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--regen-erp", action="store_true")
    args = ap.parse_args()

    con = duckdb.connect(str(WAREHOUSE))
    register_macros(con)
    for s in ("clean", "resolution", "ontology", "writeback", "meta"):
        con.execute(f"create schema if not exists {s}")

    step("fetch: Companies House")
    ch_parquet = companies_house.to_parquet(companies_house.fetch())
    step("fetch: HMRC importers, OTS, reference")
    imp = hmrc.fetch_importers()
    ots = hmrc.fetch_ots()
    com, cty = hmrc.fetch_reference()
    step("fetch: GDACS, UK sanctions")
    gd, sanc = risk.fetch_gdacs(), risk.fetch_sanctions()

    step("clean: all sources")
    companies_house.load_clean(con, ch_parquet)
    hmrc.load_clean(con, imp, ots, com, cty)
    risk.load_clean(con, gd, sanc)
    if args.regen_erp or not (RAW / "erp" / "AP_SUPPLIERS.csv").exists():
        step("generate: synthetic ERP extract")
        erp.generate(con)
    erp.load_clean(con)

    step("resolve: HMRC traders + ERP vendors -> Companies House")
    resolve_all(con)

    step("build: ontology tables")
    build_mod.build(con)

    step("validate: ontology contract")
    onto = load_ontology()
    checks = pd.DataFrame(validate(con, onto))
    con.register("checks_df", checks)
    con.execute("create or replace table meta.validation as select *, now() as run_at from checks_df")
    dq = pd.DataFrame(build_mod.data_quality_report(con))
    con.register("dq_df", dq)
    con.execute("create or replace table meta.data_quality as select *, now() as run_at from dq_df")

    print("\n== Object counts ==")
    for o in onto.objects.values():
        print(f"  {o.name:<18} {con.execute(f'select count(*) from {o.backing_table}').fetchone()[0]:>8,}")
    print("\n== Data quality ==")
    print(dq.to_string(index=False))
    print("\n== Entity resolution vs ground truth (ERP) ==")
    print(evaluate_supplier_resolution(con, RAW / "erp" / "_ground_truth_DO_NOT_USE_IN_PIPELINE.csv"))
    failed = checks[~checks.passed]
    print(f"\n== Validation: {len(checks) - len(failed)}/{len(checks)} checks passed ==")
    if len(failed):
        print(failed.to_string(index=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
