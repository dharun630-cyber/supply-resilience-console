"""Action types: the ontology's verbs (declared in ontology.yaml -> action_types).

Every action follows the same shape, as Foundry Actions do:
  1. validate parameters against the current state of the ontology (reject with a reason)
  2. write ONLY to writeback.* tables (source-derived tables are never edited by hand)
  3. append to writeback.action_log (who did what, when, with which parameters)
  4. re-apply edits so the ontology reflects the change immediately

The UI never writes SQL; it calls these functions. The same rules then hold whether a
change comes from the dashboard, a script or (later) an automated agent.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid

import duckdb

from .transform.build import apply_edits

ALERT_TRANSITIONS = {
    "open": {"acknowledged", "closed"},
    "acknowledged": {"mitigating", "closed"},
    "mitigating": {"closed"},
    "closed": set(),
}
CRITICALITY = {"high", "medium", "low"}


class ActionRejected(ValueError):
    """Raised when an action's validation rules fail. The message is shown to the user."""


def _now():
    return dt.datetime.now().replace(microsecond=0)


def _log(con, action: str, params: dict, actor: str):
    con.execute("insert into writeback.action_log values (?, ?, ?, ?)",
                [action, json.dumps(params, default=str), actor, _now()])


def _require_vendor(con, vendor_id: str):
    if not con.execute("select 1 from ontology.erp_supplier where vendor_id = ?", [vendor_id]).fetchone():
        raise ActionRejected(f"Unknown supplier {vendor_id}")


def raise_alert(con: duckdb.DuckDBPyConnection, vendor_id: str, event_id: str, owner: str, note: str,
                actor: str) -> str:
    _require_vendor(con, vendor_id)
    if not con.execute("select 1 from ontology.risk_event where event_id = ?", [event_id]).fetchone():
        raise ActionRejected(f"Unknown event {event_id}")
    if not (owner or "").strip():
        raise ActionRejected("An alert needs an owner")
    dup = con.execute("""select alert_id from writeback.alert
                         where vendor_id = ? and event_id = ? and status <> 'closed'""",
                      [vendor_id, event_id]).fetchone()
    if dup:
        raise ActionRejected(f"An open alert already exists for this supplier and event ({dup[0]})")
    alert_id = "ALT-" + uuid.uuid4().hex[:8].upper()
    con.execute("insert into writeback.alert values (?, ?, ?, 'open', ?, ?, ?, ?)",
                [alert_id, vendor_id, event_id, owner.strip(), note or "", _now(), _now()])
    _log(con, "raise_alert", {"alert_id": alert_id, "vendor_id": vendor_id, "event_id": event_id,
                              "owner": owner, "note": note}, actor)
    return alert_id


def update_alert_status(con: duckdb.DuckDBPyConnection, alert_id: str, status: str, note: str,
                        actor: str) -> None:
    row = con.execute("select status, note from writeback.alert where alert_id = ?", [alert_id]).fetchone()
    if not row:
        raise ActionRejected(f"Unknown alert {alert_id}")
    current, old_note = row
    if status not in ALERT_TRANSITIONS[current]:
        allowed = ", ".join(sorted(ALERT_TRANSITIONS[current])) or "none (closed)"
        raise ActionRejected(f"Cannot move {current} -> {status}. Allowed: {allowed}")
    if status == "closed" and not (note or "").strip():
        raise ActionRejected("Closing an alert requires a note explaining the outcome")
    new_note = (old_note + "\n" if old_note else "") + f"[{_now():%d %b %H:%M} {actor}] {status}: {note}"
    con.execute("update writeback.alert set status = ?, note = ?, updated_at = ? where alert_id = ?",
                [status, new_note, _now(), alert_id])
    _log(con, "update_alert_status", {"alert_id": alert_id, "from": current, "to": status, "note": note}, actor)


def confirm_match(con: duckdb.DuckDBPyConnection, vendor_id: str, company_number: str, actor: str) -> None:
    _require_vendor(con, vendor_id)
    company_number = (company_number or "").strip().upper()
    if company_number.isdigit():
        company_number = company_number.zfill(8)
    if not con.execute("select 1 from clean.companies where company_number = ?", [company_number]).fetchone():
        raise ActionRejected(f"{company_number} is not a Companies House number")
    con.execute("insert or replace into writeback.match_override values (?, ?, 'confirm', ?, ?)",
                [vendor_id, company_number, actor, _now()])
    _log(con, "confirm_match", {"vendor_id": vendor_id, "company_number": company_number}, actor)
    apply_edits(con)


def reject_match(con: duckdb.DuckDBPyConnection, vendor_id: str, actor: str) -> None:
    _require_vendor(con, vendor_id)
    con.execute("insert or replace into writeback.match_override values (?, null, 'reject', ?, ?)",
                [vendor_id, actor, _now()])
    _log(con, "reject_match", {"vendor_id": vendor_id}, actor)
    apply_edits(con)


def set_criticality(con: duckdb.DuckDBPyConnection, vendor_id: str, criticality: str, actor: str) -> None:
    _require_vendor(con, vendor_id)
    if criticality not in CRITICALITY:
        raise ActionRejected(f"Criticality must be one of {sorted(CRITICALITY)}")
    con.execute("insert or replace into writeback.criticality_override values (?, ?, ?, ?)",
                [vendor_id, criticality, actor, _now()])
    _log(con, "set_criticality", {"vendor_id": vendor_id, "criticality": criticality}, actor)
    apply_edits(con)
