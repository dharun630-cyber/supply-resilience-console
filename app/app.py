"""Supply Resilience Console: an event-first operational dashboard over the ontology.

    python app/app.py   ->  http://127.0.0.1:8050

Left:   risk events (what happened)
Centre: which of OUR suppliers it hits, ranked by triage priority, and WHY (the path graph)
Right:  the selected supplier, plus the Actions a user can take (writeback)
"""
from __future__ import annotations

import dash_cytoscape as cyto
import pandas as pd
from dash import ALL, Dash, Input, Output, State, ctx, dcc, html, no_update

from service import ACTOR, console
from scm.actions import ALERT_TRANSITIONS, ActionRejected

svc = console()
app = Dash(__name__, title="Supply Resilience Console", suppress_callback_exceptions=True)

# Categorical slots (fixed order) for object types in the path graph. Every node also
# carries its type as text, so colour is never the only cue.
TYPE_COLOR = {
    "RiskEvent": "#eb6834", "Country": "#2a78d6", "ImportFlow": "#1baf7a", "Commodity": "#4a3aa7",
    "PurchaseOrderLine": "#eda100", "ERPSupplier": "#008300", "Company": "#e87ba4",
}
TYPE_ORDER = list(TYPE_COLOR)
LEVEL = {"Red": ("▲", "critical"), "Orange": ("●", "serious"), "Green": ("○", "good")}
EVENT_TYPE = {"EQ": "Earthquake", "FL": "Flood", "TC": "Tropical cyclone", "DR": "Drought",
              "VO": "Volcano", "WF": "Wildfire"}


def gbp(x) -> str:
    if x is None or pd.isna(x):
        return "–"
    return f"£{x / 1e6:.1f}m" if abs(x) >= 1e6 else f"£{x / 1e3:.0f}k"


def as_list(x) -> list:
    """DuckDB lists arrive as numpy arrays; missing values as NaN/None."""
    if x is None or isinstance(x, float):
        return []
    return [str(v) for v in x]


def text(*vals) -> str:
    for v in vals:
        if isinstance(v, str) and v:
            return v
    return ""


def level_badge(level: str):
    icon, cls = LEVEL.get(level, ("○", "good"))
    return html.Span([icon, f" {level}"], className=f"badge status-{cls}")


def alert_badge(status):
    if not status or pd.isna(status):
        return html.Span("–", className="muted")
    return html.Span(status, className=f"badge alert-{status}")


def default_selection(threshold=0.10):
    """Open on the current event with the most material suppliers, focused on its top priority."""
    ev = svc.events(current_only=False, threshold=threshold)
    cur = ev[ev.is_current]
    pool = cur if len(cur) else ev
    event = pool.sort_values("material", ascending=False).event_id.iloc[0]
    tbl = svc.exposure_table(event, threshold)
    return event, (tbl.vendor_id.iloc[0] if len(tbl) else None)


# ---------------------------------------------------------------------------- layout

def layout():
    f = svc.freshness()
    ev0, v0 = default_selection()
    return html.Div(className="shell", children=[
        html.Header(className="topbar", children=[
            html.Div([
                html.H1("Supply Resilience Console"),
                html.Div("Event → Country → Commodity → Our supplier", className="subtitle"),
            ]),
            html.Div(className="fresh", children=[
                html.Span(["Trade data ", html.B(f["trade"])]),
                html.Span(["Events pulled ", html.B(f["events"])]),
                html.Span(["Companies House ", html.B(f["companies"])]),
                html.Span(id="hdr-alerts", className="hdr-alerts"),
            ]),
        ]),
        html.Div(className="grid", children=[
            # ---------------- left: events
            html.Aside(className="col events", children=[
                html.Div(className="col-head", children=[
                    html.H2("Risk events"),
                    dcc.RadioItems(id="ev-filter", className="seg", inline=True, value="all",
                                   options=[{"label": "All recent", "value": "all"},
                                            {"label": "Current", "value": "current"}]),
                ]),
                html.Div(id="event-list", className="event-list"),
            ]),
            # ---------------- centre: tabs
            html.Main(className="col main", children=[
                html.Nav(className="tabs", children=[
                    html.Button("Exposure", id="tab-exposure", className="tab active"),
                    html.Button("Alert queue", id="tab-alerts", className="tab"),
                    html.Button("Match review", id="tab-review", className="tab"),
                    html.Button("Data health", id="tab-health", className="tab"),
                ]),
                html.Section(id="pane-exposure", children=[
                    html.Div(id="ev-header"),
                    html.Div(id="kpis", className="kpis"),
                    html.Div(className="controls", children=[
                        html.Label("Materiality threshold: show suppliers with exposure ≥", htmlFor="threshold"),
                        dcc.Slider(id="threshold", min=0, max=0.5, step=0.05, value=0.10,
                                   marks={v / 100: f"{v / 100:.2f}" for v in range(0, 51, 10)}),
                    ]),
                    html.Div(id="exp-table", className="table-wrap"),
                    html.H3("Why is this supplier exposed?", className="section-title"),
                    html.Div(id="path-caption", className="caption"),
                    html.Div(className="path-card", children=[
                        cyto.Cytoscape(id="path-graph", layout={"name": "preset", "fit": True, "padding": 24},
                                       style={"width": "100%", "height": "560px"},
                                       userZoomingEnabled=False, userPanningEnabled=False,
                                       autoungrabify=True, elements=[], stylesheet=CYTO_STYLE),
                        html.Div(className="legend", children=[
                            html.Span([html.I(style={"borderColor": c}), t]) for t, c in TYPE_COLOR.items()
                        ]),
                    ]),
                    html.Div(id="breakdown", className="table-wrap"),
                ]),
                html.Section(id="pane-alerts", hidden=True, children=[html.Div(id="alerts-body")]),
                html.Section(id="pane-review", hidden=True, children=[html.Div(id="review-body")]),
                html.Section(id="pane-health", hidden=True, children=[html.Div(id="health-body")]),
            ]),
            # ---------------- right: supplier + actions
            html.Aside(className="col panel", children=[
                html.Div(id="panel-body"),
                html.Div(id="panel-actions", className="actions", children=[
                    html.H3("Actions", className="section-title"),
                    html.Div(className="action", children=[
                        html.H4("Raise alert for this event"),
                        dcc.Input(id="al-owner", value=ACTOR, placeholder="Owner", className="input"),
                        dcc.Textarea(id="al-note", placeholder="What should happen next?", className="input"),
                        html.Button("Raise alert", id="btn-alert", className="btn primary"),
                    ]),
                    html.Div(className="action", children=[
                        html.H4("Move an alert"),
                        dcc.Input(id="mv-note", placeholder="Note (required to close)", className="input"),
                        html.Div(id="mv-buttons", className="btn-row"),
                    ]),
                    html.Div(className="action", children=[
                        html.H4("Supplier criticality"),
                        html.Div(className="btn-row", children=[
                            dcc.Dropdown(id="crit", options=["high", "medium", "low"], clearable=False,
                                         className="dd"),
                            html.Button("Set", id="btn-crit", className="btn"),
                        ]),
                    ]),
                    html.Div(className="action", children=[
                        html.H4("Entity match"),
                        dcc.Input(id="match-co", placeholder="Company number (blank = confirm current)",
                                  className="input"),
                        html.Div(className="btn-row", children=[
                            html.Button("Confirm", id="btn-confirm", className="btn"),
                            html.Button("Reject match", id="btn-reject", className="btn danger"),
                        ]),
                    ]),
                ]),
            ]),
        ]),
        html.Div(id="toast", className="toast", hidden=True),
        dcc.Store(id="sel-event", data=ev0),
        dcc.Store(id="sel-vendor", data=v0),
        dcc.Store(id="version", data=0),
    ])


CYTO_STYLE = [
    {"selector": "node", "style": {
        "shape": "round-rectangle", "width": 180, "height": 46,
        "background-color": "#fcfcfb", "border-width": 3, "border-color": "data(color)",
        "label": "data(label)", "text-wrap": "wrap", "text-max-width": 170, "font-size": 11,
        "color": "#0b0b0b", "text-valign": "center", "text-halign": "center",
        "font-family": "Inter, system-ui, sans-serif"}},
    {"selector": "node.focus", "style": {"border-width": 5}},
    {"selector": "edge", "style": {
        "width": 2, "line-color": "#b9b8b2", "target-arrow-color": "#b9b8b2",
        "target-arrow-shape": "triangle", "curve-style": "bezier", "arrow-scale": 0.9,
        "label": "data(label)", "font-size": 10, "color": "#52514e",
        "text-background-color": "#fcfcfb", "text-background-opacity": 1, "text-background-padding": 2}},
]

app.layout = layout


# ---------------------------------------------------------------------------- tabs

TABS = ["exposure", "alerts", "review", "health"]


@app.callback([Output(f"pane-{t}", "hidden") for t in TABS] + [Output(f"tab-{t}", "className") for t in TABS],
              [Input(f"tab-{t}", "n_clicks") for t in TABS] + [Input("sel-vendor", "data")],
              prevent_initial_call=True)
def switch_tab(*_):
    trig = ctx.triggered_id
    # picking a supplier from the queue/review tabs jumps back to the exposure view
    active = trig.replace("tab-", "") if isinstance(trig, str) and trig.startswith("tab-") else "exposure"
    return [t != active for t in TABS] + [f"tab{' active' if t == active else ''}" for t in TABS]


# ---------------------------------------------------------------------------- selection

@app.callback(Output("sel-event", "data"), Output("sel-vendor", "data"),
              Input({"type": "ev", "index": ALL}, "n_clicks"),
              Input({"type": "row", "index": ALL}, "n_clicks"),
              Input({"type": "pick", "v": ALL, "e": ALL}, "n_clicks"),
              Input("sel-event", "data"), Input("threshold", "value"),
              State("sel-vendor", "data"))
def select(_ev, _row, _pick, sel_event, threshold, sel_vendor):
    trig = ctx.triggered_id
    if isinstance(trig, dict) and not ctx.triggered[0]["value"]:
        return no_update, no_update  # component just rendered, not clicked
    if isinstance(trig, dict) and trig["type"] == "row":
        return no_update, trig["index"]
    if isinstance(trig, dict) and trig["type"] == "pick":
        return (trig["e"] or no_update), trig["v"]
    event = trig["index"] if isinstance(trig, dict) and trig["type"] == "ev" else sel_event
    # New event (or first load): focus its top-priority supplier
    tbl = svc.exposure_table(event, threshold or 0)
    tbl = tbl[tbl.exposure_score >= (threshold or 0)] if len(tbl) else tbl
    if trig == "threshold" and sel_vendor in set(tbl.get("vendor_id", [])):
        return no_update, no_update
    top = tbl.vendor_id.iloc[0] if len(tbl) else None
    return (event if event != sel_event else no_update), top


# ---------------------------------------------------------------------------- events list

@app.callback(Output("event-list", "children"),
              Input("ev-filter", "value"), Input("threshold", "value"),
              Input("sel-event", "data"), Input("version", "data"))
def render_events(flt, threshold, sel_event, _v):
    ev = svc.events(current_only=(flt == "current"), threshold=threshold or 0)
    if ev.empty:
        return html.P("No current events.", className="muted")
    items = []
    for r in ev.itertuples():
        items.append(html.Button(id={"type": "ev", "index": r.event_id},
                                 className="event" + (" active" if r.event_id == sel_event else ""), children=[
            html.Div(className="event-top", children=[
                level_badge(r.alert_level),
                html.Span("current" if r.is_current else f"{pd.Timestamp(r.start_date):%d %b}",
                          className="pill" + (" live" if r.is_current else "")),
            ]),
            html.Div(r.name, className="event-name"),
            html.Div(f"{EVENT_TYPE.get(r.event_type, r.event_type)} · {r.n_countries} "
                     f"{'country' if r.n_countries == 1 else 'countries'}", className="muted small"),
            html.Div([html.B(r.material), " material suppliers"], className="event-count"),
        ]))
    return items


# ---------------------------------------------------------------------------- exposure

@app.callback(Output("ev-header", "children"), Output("kpis", "children"), Output("exp-table", "children"),
              Input("sel-event", "data"), Input("threshold", "value"),
              Input("sel-vendor", "data"), Input("version", "data"))
def render_exposure(event_id, threshold, sel_vendor, _v):
    if not event_id:
        return "", "", ""
    threshold = threshold or 0
    e = svc.event(event_id)
    header = html.Div(className="ev-header", children=[
        html.Div([level_badge(e["alert_level"]),
                  html.Span("current" if e["is_current"] else "past event, replay",
                            className="pill" + (" live" if e["is_current"] else ""))], className="row"),
        html.H2(e["name"]),
        html.Div([f"{EVENT_TYPE.get(e['event_type'], e['event_type'])} · "
                  f"{pd.Timestamp(e['start_date']):%d %b %Y} – {pd.Timestamp(e['end_date']):%d %b %Y} · "
                  f"affects {e['countries']} · ",
                  html.A("GDACS report ↗", href=e["url"], target="_blank")], className="muted"),
    ])
    df = svc.exposure_table(event_id, threshold)
    if df.empty:
        return header, "", html.P("This event reaches none of our suppliers.", className="muted")
    mat = df[df.exposure_score >= threshold]
    open_alerts = int(mat.alert_status.isin(["open", "acknowledged", "mitigating"]).sum())
    kpis = [
        tile("Suppliers reached", f"{len(df)}", "any path from the event"),
        tile("Material", f"{len(mat)}", f"exposure ≥ {threshold:.2f}"),
        tile("High-criticality material", f"{int((mat.criticality == 'high').sum())}", "per ERP / overrides"),
        tile("Exposure-weighted spend", gbp((mat.spend_12m_gbp * mat.exposure_score).sum()),
             "Σ 12-month spend × exposure"),
        tile("Alerts in flight", f"{open_alerts} / {len(mat)}", "material suppliers with an open alert"),
    ]
    rows = [html.Tr([html.Th(h) for h in
                     ["#", "Supplier", "Criticality", "Exposure", "Fragility", "Priority", "Main driver", "Alert"]])]
    for i, r in enumerate(mat.head(60).itertuples(), 1):
        frag = r.fragility_score if pd.notna(r.fragility_score) else None
        reasons = ", ".join(as_list(r.fragility_reasons))
        rows.append(html.Tr(id={"type": "row", "index": r.vendor_id}, n_clicks=0,
                            className="clickable" + (" selected" if r.vendor_id == sel_vendor else ""), children=[
            html.Td(i, className="num muted"),
            html.Td([html.Div(r.vendor_name, className="strong"),
                     html.Div([r.vendor_id, " · ", html.Span(r.path_type, className=f"tag {r.path_type}")],
                              className="muted small")]),
            html.Td(r.criticality, className=f"crit crit-{r.criticality}"),
            html.Td(bar(r.exposure_score)),
            html.Td(html.Span("unresolved" if frag is None else f"{frag:.2f}", title=reasons or None,
                              className="num" + (" warn" if (frag or 0) >= 0.3 else "") + (" muted" if frag is None else ""))),
            html.Td(f"{r.priority:.2f}", className="num strong"),
            html.Td([html.Div(text(r.hs4_description, r.driver_commodity)[:48]),
                     html.Div(f"via {r.driver_country_name}", className="muted small")]),
            html.Td(alert_badge(r.alert_status)),
        ]))
    hidden = len(df) - len(mat)
    table = [html.Table(rows, className="tbl"),
             html.P(f"{hidden} more suppliers are reached with exposure below {threshold:.2f}. "
                    "Lower the threshold to see them." if hidden else "", className="muted small")]
    return header, kpis, table


def tile(label, value, sub):
    return html.Div(className="tile", children=[html.Div(label, className="tile-label"),
                                                html.Div(value, className="tile-value"),
                                                html.Div(sub, className="tile-sub")])


def bar(x):
    return html.Div(className="bar", title=f"exposure {x:.3f}", children=[
        html.Div(className="bar-track", children=[html.Div(className="bar-fill", style={"width": f"{x * 100:.1f}%"})]),
        html.Span(f"{x:.2f}", className="num"),
    ])


# ---------------------------------------------------------------------------- path graph

@app.callback(Output("path-graph", "elements"), Output("path-caption", "children"), Output("breakdown", "children"),
              Input("sel-event", "data"), Input("sel-vendor", "data"), Input("version", "data"))
def render_path(event_id, vendor_id, _v):
    if not (event_id and vendor_id):
        return [], "Select a supplier to see the chain of objects linking it to the event.", ""
    bd = [r for r in svc.breakdown(event_id, vendor_id)]
    s = svc.supplier(vendor_id)
    e = svc.event(event_id)
    hit = [r for r in bd if r["contribution"] > 0][:3]
    cols: dict[str, list] = {t: [] for t in TYPE_ORDER}
    edges = []

    def node(t, key, label, focus=False):
        n = f"{t}:{key}"
        if n not in [x[0] for x in cols[t]]:
            cols[t].append((n, label, focus))
        return n

    ev = node("RiskEvent", event_id, f"RiskEvent\n{e['name']}")
    sup = node("ERPSupplier", vendor_id, f"ERPSupplier\n{s['vendor_name'][:34]}", focus=True)
    direct = s["country_iso3"] != "GBR"
    for r in hit:
        com = node("Commodity", r["cn8_code"], f"Commodity {r['cn8_code']}\n{r['description'][:40]}")
        po = node("PurchaseOrderLine", r["cn8_code"], f"PurchaseOrderLine ×{r['po_lines']}\n{gbp(r['spend'])} "
                                                       f"({r['weight']:.0%} of our spend)")
        edges += [(po, com, "po_commodity"), (po, sup, "ordered_from")]
        for c in r["countries"][:3]:
            k = node("Country", c["iso3"], f"Country\n{c['country']}")
            edges.append((ev, k, "affects"))
            if direct:
                edges.append((sup, k, "located_in"))
            else:
                fl = node("ImportFlow", c["flow_id"], f"ImportFlow\n{c['share']:.0%} of UK imports · {gbp(c['value_gbp'])}")
                edges += [(fl, k, "flow_from_country"), (fl, com, "flow_of_commodity")]
    if pd.notna(s.get("company_number")):
        co = node("Company", s["company_number"], f"Company {s['company_number']}\n{(s['company_name'] or '')[:34]}")
        conf = s["confidence"]
        edges.append((sup, co, f"resolves_to ({conf:.2f})" if pd.notna(conf) else "resolves_to"))

    present = [t for t in TYPE_ORDER if cols[t]]
    elements = []
    for x, t in enumerate(present):
        items = cols[t]
        for y, (n, label, focus) in enumerate(items):
            # Top-to-bottom: one row per object type, so the chain reads like a sentence
            elements.append({"data": {"id": n, "label": label, "color": TYPE_COLOR[t]},
                             "position": {"x": (y - (len(items) - 1) / 2) * 200, "y": x * 82},
                             "classes": "focus" if focus else ""})
    seen = set()
    for a, b, lbl in edges:
        if (a, b) not in seen:
            seen.add((a, b))
            elements.append({"data": {"source": a, "target": b, "label": lbl}})

    total = sum(r["contribution"] for r in bd)
    if direct:
        caption = (f"Direct path: {s['vendor_name']} ships from {hit[0]['countries'][0]['country'] if hit else 'n/a'}, "
                   f"which the event affects. Exposure = event severity ({e['severity']:.1f}) × share of our spend "
                   f"with this supplier on affected lines.")
    else:
        caption = ("Inferred path: HMRC shows which commodities UK importers buy, not from where. We apply the "
                   "UK's national sourcing mix for each commodity, weighted by what we buy from this supplier. "
                   f"Exposure = Σ (our spend share × affected countries' UK import share × severity {e['severity']:.1f}).")
    rows = [html.Tr([html.Th(h) for h in ["Commodity", "Our spend", "Weight", "Event risk on commodity",
                                          "Contribution", "Driven by"]])]
    for r in bd[:8]:
        rows.append(html.Tr([
            html.Td([html.Div(r["cn8_code"], className="num"), html.Div(r["description"][:60], className="muted small")]),
            html.Td(gbp(r["spend"]), className="num"),
            html.Td(f"{r['weight']:.0%}", className="num"),
            html.Td(f"{r['risk']:.3f}", className="num"),
            html.Td(f"{r['contribution']:.3f}", className="num strong"),
            html.Td(", ".join(f"{c['country']} {c['share']:.0%}" for c in r["countries"][:3]) or "–",
                    className="small"),
        ]))
    rows.append(html.Tr([html.Td("Exposure score", className="strong"), html.Td(), html.Td(), html.Td(),
                         html.Td(f"{min(total, 1):.3f}", className="num strong"), html.Td()], className="total"))
    return elements, caption, html.Table(rows, className="tbl compact")


# ---------------------------------------------------------------------------- supplier panel

@app.callback(Output("panel-body", "children"), Output("crit", "value"), Output("mv-buttons", "children"),
              Output("panel-actions", "hidden"),
              Input("sel-vendor", "data"), Input("sel-event", "data"), Input("version", "data"))
def render_panel(vendor_id, event_id, _v):
    if not vendor_id:
        return html.P("Select a supplier.", className="muted"), None, [], True
    s = svc.supplier(vendor_id)
    reasons = as_list(s.get("fragility_reasons"))
    resolved = pd.notna(s.get("company_number"))
    method = s.get("match_method") or ""
    body = [
        html.Div("ERPSupplier", className="type-label"),
        html.H2(s["vendor_name"]),
        html.Div(f"{vendor_id} · {s['country']} · {s['postcode'] or 'no postcode'} · spend {gbp(s['spend_12m_gbp'])}/yr",
                 className="muted small"),
        html.Dl(className="props", children=[
            html.Dt("Criticality"), html.Dd(s["criticality"], className=f"crit crit-{s['criticality']}"),
            html.Dt("Stated reg. no."), html.Dd(s["stated_company_number"] or "–", className="num"),
        ]),
        html.H3("Resolves to", className="section-title"),
    ]
    if resolved:
        body += [
            html.Div(className="company", children=[
                html.Div("Company", className="type-label"),
                html.Div(s["company_name"], className="strong"),
                html.Div([html.Span(s["company_number"], className="num"), f" · {s['company_status']}",
                          f" · inc. {pd.Timestamp(s['incorporated_on']):%Y}" if pd.notna(s['incorporated_on']) else ""],
                         className="muted small"),
                html.Div(className="match", children=[
                    html.Span(method.replace("_", " "), className="tag"),
                    html.Span(f"score {s['match_score']:.0f}" if pd.notna(s["match_score"]) else ""),
                    html.Span(f"confidence {s['confidence']:.2f}" if pd.notna(s["confidence"]) else ""),
                    html.Span("⚑ needs review", className="badge status-serious") if s["needs_review"] else None,
                ]),
                html.Div(className="frag", children=[
                    html.Span("Fragility "), html.B(f"{s['fragility_score']:.2f}"),
                    html.Span(" (" + "; ".join(reasons) + ")" if reasons else " (no warning signs)", className="muted"),
                ]),
                html.Div(f"⚠ {int(s['duplicate_vendors'])} other vendor record(s) resolve to this company "
                         "(probable duplicate in the ERP)", className="note") if s["duplicate_vendors"] else None,
            ])
        ]
    else:
        body.append(html.Div(
            "No Companies House match: overseas supplier or unresolved. Enter a company number below to link it.",
            className="note"))

    alerts = s["alerts"]
    body.append(html.H3("Alerts", className="section-title"))
    mv = []
    if alerts.empty:
        body.append(html.P("No alerts raised for this supplier.", className="muted small"))
    for a in alerts.itertuples():
        body.append(html.Div(className="alert-card", children=[
            html.Div([alert_badge(a.status), html.Span(f" {a.alert_id} · {a.event_name}", className="small")]),
            html.Div(f"Owner {a.owner} · raised {pd.Timestamp(a.created_at):%d %b %H:%M}", className="muted small"),
            html.Pre(a.note, className="alert-note") if a.note else None,
        ]))
        if a.event_id == event_id:
            mv += [html.Button(f"{a.alert_id} → {to}", className="btn small",
                               id={"type": "mv", "alert": a.alert_id, "to": to})
                   for to in sorted(ALERT_TRANSITIONS[a.status])]
    if not mv:
        mv = [html.Span("No open alert for this event.", className="muted small")]
    return body, s["criticality"], mv, False


# ---------------------------------------------------------------------------- actions

@app.callback(Output("version", "data"), Output("toast", "children"), Output("toast", "hidden"),
              Output("toast", "className"), Output("al-note", "value"), Output("mv-note", "value"),
              Output("match-co", "value"),
              Input("btn-alert", "n_clicks"), Input("btn-crit", "n_clicks"),
              Input("btn-confirm", "n_clicks"), Input("btn-reject", "n_clicks"),
              Input({"type": "mv", "alert": ALL, "to": ALL}, "n_clicks"),
              State("sel-vendor", "data"), State("sel-event", "data"), State("al-owner", "value"),
              State("al-note", "value"), State("crit", "value"), State("match-co", "value"),
              State("mv-note", "value"), State("version", "data"),
              prevent_initial_call=True)
def run_action(_a, _c, _cf, _rj, _mv, vendor, event, owner, note, crit, match_co, mv_note, version):
    trig = ctx.triggered_id
    if not ctx.triggered[0]["value"]:
        return (no_update,) * 7
    try:
        if trig == "btn-alert":
            aid = svc.act("raise_alert", vendor_id=vendor, event_id=event, owner=owner, note=note)
            msg = f"Alert {aid} raised and assigned to {owner}."
        elif trig == "btn-crit":
            svc.act("set_criticality", vendor_id=vendor, criticality=crit)
            msg = f"Criticality set to {crit}. Priorities re-ranked."
        elif trig == "btn-confirm":
            s = svc.supplier(vendor)
            co = (match_co or "").strip() or s.get("company_number")
            if not co or pd.isna(co):
                raise ActionRejected("Nothing to confirm. Enter a company number.")
            svc.act("confirm_match", vendor_id=vendor, company_number=co)
            msg = f"Match confirmed: {vendor} → {co}. This survives pipeline rebuilds."
        elif trig == "btn-reject":
            svc.act("reject_match", vendor_id=vendor)
            msg = f"Match rejected for {vendor}."
        elif isinstance(trig, dict) and trig["type"] == "mv":
            svc.act("update_alert_status", alert_id=trig["alert"], status=trig["to"], note=mv_note or "")
            msg = f"{trig['alert']} moved to {trig['to']}."
        else:
            return (no_update,) * 7
        return version + 1, "✓ " + msg, False, "toast ok", "", "", ""
    except ActionRejected as e:
        return no_update, "✕ Rejected: " + str(e), False, "toast err", no_update, no_update, no_update


# ---------------------------------------------------------------------------- other tabs

@app.callback(Output("hdr-alerts", "children"), Output("alerts-body", "children"),
              Input("version", "data"))
def render_alerts(_v):
    a = svc.alerts()
    live = a[a.status != "closed"]
    hdr = [html.B(len(live)), " open alerts"]
    if a.empty:
        return hdr, html.P("No alerts yet. Raise one from the Exposure view.", className="muted")
    rows = [html.Tr([html.Th(h) for h in ["Alert", "Status", "Supplier", "Criticality", "Event", "Owner", "Updated"]])]
    for r in a.itertuples():
        rows.append(html.Tr(id={"type": "pick", "v": r.vendor_id, "e": r.event_id}, n_clicks=0, className="clickable",
                            children=[html.Td(r.alert_id, className="num"), html.Td(alert_badge(r.status)),
                                      html.Td(r.vendor_name), html.Td(r.criticality, className=f"crit crit-{r.criticality}"),
                                      html.Td([level_badge(r.alert_level), " ", r.event_name]), html.Td(r.owner),
                                      html.Td(f"{pd.Timestamp(r.updated_at):%d %b %H:%M}", className="num")]))
    return hdr, [html.H2("Alert queue"),
                 html.P("Every alert is an object created by an Action. Click one to open it.", className="muted"),
                 html.Table(rows, className="tbl")]


@app.callback(Output("review-body", "children"), Input("version", "data"))
def render_review(_v):
    r = svc.review_queue()
    head = [html.H2("Match review"),
            html.P("Vendor records the resolver linked with medium confidence. Confirm or reject each one; "
                   "decisions are stored as writeback and re-applied on every rebuild.", className="muted")]
    if r.empty:
        return head + [html.P("Queue empty. Every match is confirmed or high-confidence.", className="muted")]
    rows = [html.Tr([html.Th(h) for h in ["ERP vendor", "ERP postcode", "Matched company", "Company postcode",
                                          "Method", "Score", "Conf."]])]
    for x in r.itertuples():
        rows.append(html.Tr(id={"type": "pick", "v": x.vendor_id, "e": ""}, n_clicks=0, className="clickable",
                            children=[html.Td([html.Div(x.vendor_name), html.Div(x.vendor_id, className="muted small")]),
                                      html.Td(x.postcode or "–"),
                                      html.Td([html.Div(x.company_name), html.Div(x.company_number, className="muted small")]),
                                      html.Td(x.company_postcode or "–"), html.Td(x.match_method.replace("_", " ")),
                                      html.Td(f"{x.match_score:.0f}", className="num"),
                                      html.Td(f"{x.confidence:.2f}", className="num")]))
    return head + [html.Table(rows, className="tbl")]


@app.callback(Output("health-body", "children"), Input("version", "data"))
def render_health(_v):
    checks, dq, log = svc.health()
    passed = int(checks.passed.sum())
    return [
        html.H2("Data health"),
        html.Div(className="kpis", children=[
            tile("Ontology contract", f"{passed}/{len(checks)}", "checks passed at last build"),
            tile("Object types", f"{len(svc.onto.objects)}", "declared in ontology.yaml"),
            tile("Link types", f"{len(svc.onto.links)}", "declared in ontology.yaml"),
            tile("Graph", f"{svc.G.number_of_nodes():,}", f"nodes · {svc.G.number_of_edges():,} edges"),
        ]),
        html.H3("Source data quality", className="section-title"),
        html.Table([html.Tr([html.Th("Metric"), html.Th("Value")])] +
                   [html.Tr([html.Td(r.metric), html.Td(f"{r.value:,}", className="num")]) for r in dq.itertuples()],
                   className="tbl compact"),
        html.H3("Failed checks", className="section-title"),
        html.Table([html.Tr([html.Td(r.check), html.Td(r.detail)]) for r in checks[~checks.passed].itertuples()],
                   className="tbl compact") if passed < len(checks) else html.P("None.", className="muted"),
        html.H3("Recent actions (audit log)", className="section-title"),
        html.Table([html.Tr([html.Th(h) for h in ["When", "Actor", "Action", "Parameters"]])] +
                   [html.Tr([html.Td(f"{pd.Timestamp(r.logged_at):%d %b %H:%M:%S}", className="num"), html.Td(r.actor),
                             html.Td(r.action), html.Td(html.Code(r.params), className="small")])
                    for r in log.itertuples()], className="tbl compact") if len(log) else
        html.P("No actions yet.", className="muted"),
    ]


if __name__ == "__main__":
    app.run(debug=False, port=8050)
