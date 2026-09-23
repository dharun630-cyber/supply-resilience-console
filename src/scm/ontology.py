"""Load the ontology definition and validate ontology tables against it.

Validation is the ontology acting as a *contract*: if a pipeline change breaks a
primary key or orphans a link, the build fails loudly instead of the dashboard
quietly showing wrong numbers.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import duckdb
import yaml

from .config import ONTOLOGY_YAML


@dataclass
class ObjectType:
    name: str
    backing_table: str
    primary_key: str
    title_property: str
    properties: dict[str, dict]
    writeback: bool = False


@dataclass
class LinkType:
    name: str
    from_type: str
    to_type: str
    cardinality: str
    backing_table: str
    from_key: str
    to_key: str
    properties: list[str] = field(default_factory=list)


@dataclass
class Ontology:
    objects: dict[str, ObjectType]
    links: dict[str, LinkType]
    actions: dict[str, dict]


def load_ontology(path=ONTOLOGY_YAML) -> Ontology:
    spec = yaml.safe_load(open(path))
    objects = {
        name: ObjectType(
            name=name,
            backing_table=o["backing_table"],
            primary_key=o["primary_key"],
            title_property=o["title_property"],
            properties=o["properties"],
            writeback=o.get("writeback", False),
        )
        for name, o in spec["object_types"].items()
    }
    links = {
        name: LinkType(
            name=name,
            from_type=l["from"],
            to_type=l["to"],
            cardinality=l["cardinality"],
            backing_table=l["backing_table"],
            from_key=l["from_key"],
            to_key=l["to_key"],
            properties=l.get("properties", []),
        )
        for name, l in spec["link_types"].items()
    }
    for l in links.values():
        assert l.from_type in objects and l.to_type in objects, f"link {l.name} references unknown type"
    return Ontology(objects, links, spec.get("action_types", {}))


def validate(con: duckdb.DuckDBPyConnection, onto: Ontology) -> list[dict]:
    """Run contract checks. Returns a list of check results (also persisted by the pipeline)."""
    results = []

    def check(name, ok, detail=""):
        results.append({"check": name, "passed": bool(ok), "detail": str(detail)})

    for o in onto.objects.values():
        t, pk = o.backing_table, o.primary_key
        cols = {r[0] for r in con.execute(f"describe {t}").fetchall()}
        missing = set(o.properties) - cols
        check(f"{o.name}: declared properties exist", not missing, missing or "")
        n, n_distinct, n_null = con.execute(
            f"select count(*), count(distinct {pk}), count(*) - count({pk}) from {t}"
        ).fetchone()
        check(f"{o.name}: primary key unique", n == n_distinct + n_null, f"{n} rows")
        check(f"{o.name}: primary key not null", n_null == 0, f"{n_null} nulls")

    for l in onto.links.values():
        src, dst = onto.objects[l.from_type], onto.objects[l.to_type]
        # Every link endpoint must resolve to a real object (referential integrity).
        for side, key, obj in (("from", l.from_key, src), ("to", l.to_key, dst)):
            orphans = con.execute(
                f"""select count(*) from {l.backing_table} x
                    where x.{key} is not null
                      and x.{key} not in (select {obj.primary_key} from {obj.backing_table})"""
            ).fetchone()[0]
            check(f"link {l.name}: {side} endpoints exist", orphans == 0, f"{orphans} orphans")
        if l.cardinality == "many_to_one":
            dupes = con.execute(
                f"""select count(*) from (select {l.from_key} from {l.backing_table}
                    where {l.to_key} is not null group by 1 having count(distinct {l.to_key}) > 1)"""
            ).fetchone()[0]
            check(f"link {l.name}: many_to_one respected", dupes == 0, f"{dupes} violations")
    return results
