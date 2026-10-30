"""The object tree:

    Servers > Server > Databases > Database > Schemas > Schema >
        Tables > Table > Columns / Constraints / Indexes / Triggers
        Views > View > Columns
        Materialized Views > Materialized View > Columns / Indexes
        Functions, Procedures, Sequences, Types / Domains

Nodes are plain dicts. A node's identity is its type plus the server / db / schema / table (a relation such as a table,
view or sequence) / name (a type or trigger) / oid (a function or procedure) it points at, so the browser can ask for any
node's children or details with those few query parameters. `rel` says which kind of relation a Columns / Indexes /
Triggers folder belongs to, and `system=1` asks for system and extension objects too.
"""
from servers.models import ServerConnection

from . import pg

NODE_KEYS = ("server", "db", "schema", "table", "name", "oid", "rel")

SCHEMA_FOLDERS = (("Tables", "tables"), ("Views", "views"), ("Materialized Views", "matviews"), ("Functions", "functions"),
                  ("Procedures", "procedures"), ("Sequences", "sequences"), ("Types / Domains", "types"))
RELATION_KINDS = {"tables": ("table", "partitioned table"), "views": ("view",), "matviews": ("materialized view",),
                  "sequences": ("sequence",)}
RELATION_NODE = {"tables": "table", "views": "view", "matviews": "matview", "sequences": "sequence"}


def make_node(label, type, expandable=True, hint="", **location):
    node = {"label": label, "type": type, "expandable": expandable, "hint": hint}
    node.update({key: str(location.get(key, "")) for key in NODE_KEYS})
    return node


def parse_node(params):
    node = {"type": params.get("type") or "servers", "system": params.get("system") == "1"}
    node.update({key: params.get(key, "") for key in NODE_KEYS})
    return node


def get_server(node):
    """The saved server a node belongs to, or None when the id is missing or unknown."""
    try:
        return ServerConnection.objects.filter(pk=int(node["server"])).first()
    except (TypeError, ValueError):
        return None


def children(node):
    kind = node["type"]
    if kind == "servers":
        # No host:port hint here: the sidebar is narrow and the server's details pane already shows it.
        return [make_node(s.name, "server", server=s.pk) for s in ServerConnection.objects.all()]
    server = get_server(node)
    if server is None:
        raise pg.PgError("That server is not saved in PgDeck.")
    db, schema, table, system = node["db"], node["schema"], node["table"], node["system"]
    sid = server.pk
    if kind == "server":
        return [make_node("Databases", "databases", server=sid)]
    if kind == "databases":
        with pg.connect(server) as conn:
            return [make_node(d["name"], "database", hint=d["size_text"], server=sid, db=d["name"]) for d in pg.list_databases(conn)]
    if kind == "database":
        return [make_node("Schemas", "schemas", server=sid, db=db)]
    if kind == "schemas":
        with pg.connect(server, db) as conn:
            return [make_node(s["name"], "schema", server=sid, db=db, schema=s["name"]) for s in pg.schema_rows(conn, system)]
    if kind == "schema":
        return [make_node(label, folder, server=sid, db=db, schema=schema) for label, folder in SCHEMA_FOLDERS]

    where = {"server": sid, "db": db, "schema": schema}
    if kind in RELATION_KINDS:
        with pg.connect(server, db) as conn:
            relations = [r for r in pg.list_tables(conn, schema, system) if r["kind"] in RELATION_KINDS[kind]]
        node_type = RELATION_NODE[kind]
        return [make_node(r["name"], node_type, expandable=node_type != "sequence",
                          hint="" if r["estimate"] is None or node_type in ("view", "sequence") else f"~{r['estimate']} rows",
                          table=r["name"], **where) for r in relations]
    if kind in ("functions", "procedures"):
        with pg.connect(server, db) as conn:
            items = pg.list_functions(conn, schema, procedures=kind == "procedures", system=system)
        return [make_node(f["signature"], "procedure" if kind == "procedures" else "function", expandable=False,
                          hint=f["returns"], oid=f["oid"], **where) for f in items]
    if kind == "types":
        with pg.connect(server, db) as conn:
            return [make_node(t["name"], "type", expandable=False, hint=t["kind"], name=t["name"], **where)
                    for t in pg.list_types(conn, schema, system)]

    place = dict(where, table=table)
    if kind == "table":
        return [make_node(label, folder, rel="table", **place) for label, folder in
                (("Columns", "columns"), ("Constraints", "constraints"), ("Indexes", "indexes"), ("Triggers", "triggers"))]
    if kind == "view":
        return [make_node("Columns", "columns", rel="view", **place)]
    if kind == "matview":
        return [make_node("Columns", "columns", rel="matview", **place), make_node("Indexes", "indexes", rel="matview", **place)]
    rel = node["rel"] or "table"
    if kind == "columns":
        with pg.connect(server, db) as conn:
            return [make_node(f"{c['name']} : {c['type']}", "column", expandable=False,
                              hint="" if c["nullable"] else "not null", rel=rel, **place)
                    for c in pg.table_columns(conn, schema, table)]
    if kind == "constraints":
        with pg.connect(server, db) as conn:
            return [make_node(c["name"], "constraint", expandable=False, hint=c["type"].lower(), rel=rel, **place)
                    for c in pg.table_constraints(conn, schema, table)]
    if kind == "indexes":
        with pg.connect(server, db) as conn:
            return [make_node(i["name"], "index", expandable=False,
                              hint="primary" if i["primary"] else ("unique" if i["unique"] else ""), rel=rel, **place)
                    for i in pg.table_indexes(conn, schema, table)]
    if kind == "triggers":
        with pg.connect(server, db) as conn:
            return [make_node(t["name"], "trigger", expandable=False, hint=f"{t['timing'].lower()} {t['events'].lower()}",
                              name=t["name"], rel=rel, **place)
                    for t in pg.table_triggers(conn, schema, table)]
    return []
