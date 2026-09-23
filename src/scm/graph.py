"""Build a NetworkX graph from the ontology tables, and traverse it.

The builder is GENERIC: it loops over whatever object and link types ontology.yaml
declares. Add a new object type to the YAML and it appears in the graph with no code
change. That's the core idea of an ontology-driven platform.

Node id convention: "<ObjectType>:<primary key>", e.g. "Country:TWN".
"""
from __future__ import annotations

from collections import defaultdict

import duckdb
import networkx as nx
import pandas as pd

from .ontology import Ontology


def nid(object_type: str, pk) -> str:
    return f"{object_type}:{pk}"


def build_graph(con: duckdb.DuckDBPyConnection, onto: Ontology) -> nx.MultiDiGraph:
    G = nx.MultiDiGraph()
    for o in onto.objects.values():
        df = con.execute(f"select * from {o.backing_table}").df()
        for rec in df.to_dict("records"):
            rec = {k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in rec.items()}
            G.add_node(nid(o.name, rec[o.primary_key]), object_type=o.name,
                       title=str(rec.get(o.title_property)), **rec)
    for l in onto.links.values():
        cols = ", ".join([l.from_key, l.to_key] + l.properties)
        df = con.execute(f"""select {cols} from {l.backing_table}
                             where {l.from_key} is not null and {l.to_key} is not null""").df()
        for rec in df.to_dict("records"):
            props = {p: rec[p] for p in l.properties}
            G.add_edge(nid(l.from_type, rec[l.from_key]), nid(l.to_type, rec[l.to_key]),
                       key=l.name, link_type=l.name, **props)
    return G


# ---- traversal helpers ---------------------------------------------------

def out_links(G, node, link_type):
    return [v for _, v, k in G.out_edges(node, keys=True) if k == link_type]


def in_links(G, node, link_type):
    return [u for u, _, k in G.in_edges(node, keys=True) if k == link_type]


def event_impact(G: nx.MultiDiGraph, event_id: str) -> pd.DataFrame:
    """Which of OUR suppliers does this event touch, how much, and along which path?

    Inferred path (UK vendors):
      RiskEvent -affects-> Country <-flow_from_country- ImportFlow -flow_of_commodity-> Commodity
               <-po_commodity- PurchaseOrderLine -ordered_from-> ERPSupplier
    Direct path (overseas vendors):
      RiskEvent -affects-> Country <-located_in- ERPSupplier
    """
    ev = nid("RiskEvent", event_id)
    sev = G.nodes[ev]["severity"]
    commodity_risk = defaultdict(float)
    commodity_via = {}
    direct = {}
    for country in out_links(G, ev, "affects"):
        for flow in in_links(G, country, "flow_from_country"):
            share = G.nodes[flow]["share_of_uk_imports"] or 0.0
            for com in out_links(G, flow, "flow_of_commodity"):
                commodity_risk[com] += share * sev
                if share * sev > commodity_via.get(com, (None, 0))[1]:
                    commodity_via[com] = (country, share * sev)
        for vendor in in_links(G, country, "located_in"):
            direct[vendor] = (sev, country)

    candidates = set(direct)
    for com in commodity_risk:
        for po in in_links(G, com, "po_commodity"):
            candidates.update(out_links(G, po, "ordered_from"))

    rows = []
    for v in candidates:
        attrs = G.nodes[v]
        spend = defaultdict(float)
        for po in in_links(G, v, "ordered_from"):
            for com in out_links(G, po, "po_commodity"):
                spend[com] += G.nodes[po]["amount_gbp"]
        total = sum(spend.values()) or 1.0
        is_uk = attrs.get("country_iso3") == "GBR"
        score, best = 0.0, (None, None, 0.0)
        for com, s in spend.items():
            r = commodity_risk.get(com, 0.0) if is_uk else direct.get(v, (0.0, None))[0]
            via = commodity_via.get(com, (None,))[0] if is_uk else direct.get(v, (None, None))[1]
            score += (s / total) * r
            if (s / total) * r > best[2]:
                best = (com, via, (s / total) * r)
        if score > 0:
            rows.append({
                "vendor_id": attrs["vendor_id"], "vendor_name": attrs["vendor_name"],
                "criticality": attrs.get("criticality"), "spend_12m_gbp": attrs.get("spend_12m_gbp"),
                "exposure_score": min(score, 1.0), "path_type": "inferred" if is_uk else "direct",
                "driver_commodity": best[0] and best[0].split(":", 1)[1],
                "driver_country": best[1] and best[1].split(":", 1)[1],
            })
    return pd.DataFrame(rows).sort_values("exposure_score", ascending=False) if rows else pd.DataFrame()


def explain_path(G: nx.MultiDiGraph, event_id: str, vendor_id: str, commodity: str, country: str) -> list[str]:
    """Human-readable chain of objects and links for one exposure, for the dashboard."""
    ev, v = G.nodes[nid("RiskEvent", event_id)], G.nodes[nid("ERPSupplier", vendor_id)]
    c = G.nodes[nid("Country", country)]
    if v.get("country_iso3") != "GBR":
        return [f"{ev['name']} ({ev['alert_level']})", f"affects {c['name']}",
                f"where {v['vendor_name']} is located (direct supplier)"]
    com_node = nid("Commodity", commodity)
    flows = [f for f in in_links(G, nid("Country", country), "flow_from_country")
             if com_node in out_links(G, f, "flow_of_commodity")]
    share = G.nodes[flows[0]]["share_of_uk_imports"] if flows else 0.0
    return [f"{ev['name']} ({ev['alert_level']})", f"affects {c['name']}",
            f"which supplies {share:.0%} of UK imports of {commodity} ({G.nodes[com_node]['description'][:50]})",
            f"which we buy from {v['vendor_name']}"]


# ---- keeping the graph in sync with Actions ----------------------------------

def refresh_object_type(G: nx.MultiDiGraph, con: duckdb.DuckDBPyConnection, onto: Ontology, object_type: str):
    """Re-read one object type's backing table and update node properties in place."""
    o = onto.objects[object_type]
    for rec in con.execute(f"select * from {o.backing_table}").df().to_dict("records"):
        rec = {k: (None if isinstance(v, float) and pd.isna(v) else v) for k, v in rec.items()}
        n = nid(o.name, rec[o.primary_key])
        if n not in G:
            G.add_node(n, object_type=o.name)
        G.nodes[n].update(title=str(rec.get(o.title_property)), **rec)


def refresh_link_type(G: nx.MultiDiGraph, con: duckdb.DuckDBPyConnection, onto: Ontology, link_type: str):
    """Drop and re-add every edge of one link type (cheap for small link types)."""
    l = onto.links[link_type]
    G.remove_edges_from([(u, v, k) for u, v, k in G.edges(keys=True) if k == link_type])
    cols = ", ".join([l.from_key, l.to_key] + l.properties)
    for rec in con.execute(f"""select {cols} from {l.backing_table}
                               where {l.from_key} is not null and {l.to_key} is not null""").df().to_dict("records"):
        G.add_edge(nid(l.from_type, rec[l.from_key]), nid(l.to_type, rec[l.to_key]),
                   key=l.name, link_type=l.name, **{p: rec[p] for p in l.properties})


# ---- explanation ------------------------------------------------------------

def exposure_breakdown(G: nx.MultiDiGraph, event_id: str, vendor_id: str) -> list[dict]:
    """Per commodity we buy from this vendor: our spend weight, the event's risk on that
    commodity, the contribution to exposure, and which affected countries drive it."""
    ev = nid("RiskEvent", event_id)
    sev = G.nodes[ev]["severity"]
    affected = set(out_links(G, ev, "affects"))
    v = nid("ERPSupplier", vendor_id)
    is_uk = G.nodes[v].get("country_iso3") == "GBR"
    home = out_links(G, v, "located_in")

    spend, n_lines = defaultdict(float), defaultdict(int)
    for po in in_links(G, v, "ordered_from"):
        for com in out_links(G, po, "po_commodity"):
            spend[com] += G.nodes[po]["amount_gbp"]
            n_lines[com] += 1
    total = sum(spend.values()) or 1.0

    rows = []
    for com, s in spend.items():
        countries = []
        if is_uk:
            for flow in in_links(G, com, "flow_of_commodity"):
                k = out_links(G, flow, "flow_from_country")[0]
                if k in affected:
                    f = G.nodes[flow]
                    countries.append({"iso3": k.split(":", 1)[1], "country": G.nodes[k]["name"],
                                      "flow_id": f["flow_id"], "share": f["share_of_uk_imports"],
                                      "value_gbp": f["value_gbp"]})
            risk = sum(c["share"] for c in countries) * sev
        else:
            risk = sev if home and home[0] in affected else 0.0
            if risk:
                countries.append({"iso3": home[0].split(":", 1)[1], "country": G.nodes[home[0]]["name"],
                                  "flow_id": None, "share": 1.0, "value_gbp": None})
        rows.append({"cn8_code": com.split(":", 1)[1], "description": G.nodes[com]["description"],
                     "spend": s, "weight": s / total, "po_lines": n_lines[com], "risk": risk,
                     "contribution": s / total * risk,
                     "countries": sorted(countries, key=lambda c: -c["share"])})
    return sorted(rows, key=lambda r: -r["contribution"])
