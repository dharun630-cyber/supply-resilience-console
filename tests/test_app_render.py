"""Every event renders in every panel without error.

Regression test: an event whose only material supplier was an unresolved overseas
vendor (no Company, so fragility reasons were pd.NA) crashed the exposure table.
Run after `python pipeline.py`, with the dashboard stopped (it holds the DB lock).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "app"))


@pytest.fixture(scope="module")
def app_module():
    import app as A  # builds the service (graph + DB connection) once
    return A


def test_every_event_renders(app_module):
    A = app_module
    events = A.svc.events(current_only=False, threshold=0).event_id.tolist()
    assert events
    for ev in events:
        tbl = A.svc.exposure_table(ev, 0)
        # A NaN priority sorts last and silently buries the supplier
        assert tbl.empty or tbl.priority.notna().all(), ev
        for thr in (0.0, 0.1):
            A.render_exposure(ev, thr, None, 0)
        # the top suppliers plus every unresolved/overseas one: the historically fragile case
        vendors = [] if tbl.empty else (
            tbl.vendor_id.head(3).tolist() + tbl[tbl.company_name.isna()].vendor_id.tolist())
        for v in vendors:
            A.render_path(ev, v, 0)
            A.render_panel(v, ev, 0)


def test_side_tabs_render(app_module):
    A = app_module
    A.render_alerts(0)
    A.render_review(0)
    A.render_health(0)
