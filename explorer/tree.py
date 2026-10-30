"""The object tree: Servers > Server > Databases > Database > Schemas > Schema > Tables > Table > Columns / Constraints / Indexes.

Nodes are plain dicts. A node's identity is its type plus the server / db / schema / table it points at, so the
browser can ask for any node's children or details with those few query parameters.
"""
from servers.models import ServerConnection

from . import pg

NODE_KEYS = ("server", "db", "schema", "table")


def make_node(label, type, expandable=True, hint="", **location):
    node = {"label": label, "type": type, "expandable": expandable, "hint": hint}
    node.update({key: str(location.get(key, "")) for key in NODE_KEYS})
    return node


def parse_node(params):
    node = {"type": params.get("type") or "servers"}
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
    db, schema, table = node["db"], node["schema"], node["table"]
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
            return [make_node(s["name"], "schema", server=sid, db=db, schema=s["name"]) for s in pg.schema_rows(conn)]
    if kind == "schema":
        return [make_node("Tables", "tables", server=sid, db=db, schema=schema)]
    if kind == "tables":
        with pg.connect(server, db) as conn:
            return [make_node(t["name"], "table", hint="" if t["estimate"] is None else f"~{t['estimate']} rows",
                              server=sid, db=db, schema=schema, table=t["name"])
                    for t in pg.list_tables(conn, schema) if t["kind"] in ("table", "partitioned table")]
    place = {"server": sid, "db": db, "schema": schema, "table": table}
    if kind == "table":
        return [make_node("Columns", "columns", **place), make_node("Constraints", "constraints", **place),
                make_node("Indexes", "indexes", **place)]
    if kind == "columns":
        with pg.connect(server, db) as conn:
            return [make_node(f"{c['name']} : {c['type']}", "column", expandable=False,
                              hint="" if c["nullable"] else "not null", **place)
                    for c in pg.table_columns(conn, schema, table)]
    if kind == "constraints":
        with pg.connect(server, db) as conn:
            return [make_node(c["name"], "constraint", expandable=False, hint=c["type"].lower(), **place)
                    for c in pg.table_constraints(conn, schema, table)]
    if kind == "indexes":
        with pg.connect(server, db) as conn:
            return [make_node(i["name"], "index", expandable=False,
                              hint="primary" if i["primary"] else ("unique" if i["unique"] else ""), **place)
                    for i in pg.table_indexes(conn, schema, table)]
    return []
