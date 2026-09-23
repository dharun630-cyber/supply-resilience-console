"""Run after `python pipeline.py`:  pytest -q"""
import sys
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from scm.config import WAREHOUSE  # noqa: E402
from scm.graph import build_graph, event_impact  # noqa: E402
from scm.ontology import load_ontology, validate  # noqa: E402
from scm.transform.scoring import supplier_exposure_sql  # noqa: E402


@pytest.fixture(scope="module")
def con():
    # Same mode as the app (read-write): DuckDB refuses mixed modes on one file in one process,
    # and test_app_render opens the app. These tests only read.
    return duckdb.connect(str(WAREHOUSE))


@pytest.fixture(scope="module")
def graph(con):
    return build_graph(con, load_ontology())


def test_ontology_contract(con):
    failed = [c for c in validate(con, load_ontology()) if not c["passed"]]
    assert not failed, failed


def test_graph_has_every_object_type(graph):
    types = {d["object_type"] for _, d in graph.nodes(data=True)}
    assert types >= set(load_ontology().objects) - {"Alert"}


def test_import_shares_are_valid(con):
    bad = con.execute("""select cn8_code, sum(share_of_uk_imports) s from ontology.import_flow
                         group by 1 having s > 1.0001""").fetchall()
    assert not bad


def test_graph_traversal_matches_sql(con, graph):
    """The same business question answered two ways must give the same answer."""
    events = [r[0] for r in con.execute("select event_id from ontology.risk_event limit 10").fetchall()]
    for ev in events:
        g = event_impact(graph, ev)
        s = con.execute(supplier_exposure_sql(ev)).df()
        s = s[s.exposure_score > 0]
        assert len(g) == len(s), ev
        if len(g):
            m = g.merge(s, on="vendor_id", suffixes=("_g", "_s"))
            assert (m.exposure_score_g - m.exposure_score_s).abs().max() < 1e-9, ev
