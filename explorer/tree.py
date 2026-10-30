"""The object tree: Server > Databases > Database > Schemas > Schema > Tables > Table > Columns / Constraints / Indexes.

Nodes are plain dicts. A node's identity is its type plus the db / schema / table it points at, so the
browser can ask for any node's children or details with those few query parameters.
"""
from . import pg

NODE_KEYS = ("db", "schema", "table")


def make_node(label, type, expandable=True, hint="", **location):
    node = {"label": label, "type": type, "expandable": expandable, "hint": hint}
    node.update({key: location.get(key, "") for key in NODE_KEYS})
    return node


def parse_node(params):
    node = {"type": params.get("type") or "root"}
    node.update({key: params.get(key, "") for key in NODE_KEYS})
    return node


def children(server, node):
    kind, db, schema, table = node["type"], node["db"], node["schema"], node["table"]
    if kind == "root":
        return [make_node("Databases", "databases")]
    if kind == "databases":
        with pg.connect(server) as conn:
            return [make_node(d["name"], "database", hint=d["size_text"], db=d["name"]) for d in pg.list_databases(conn)]
    if kind == "database":
        return [make_node("Schemas", "schemas", db=db)]
    if kind == "schemas":
        with pg.connect(server, db) as conn:
            return [make_node(s["name"], "schema", db=db, schema=s["name"]) for s in pg.schema_rows(conn)]
    if kind == "schema":
        return [make_node("Tables", "tables", db=db, schema=schema)]
    if kind == "tables":
        with pg.connect(server, db) as conn:
            return [make_node(t["name"], "table", hint=f"~{t['estimate']} rows", db=db, schema=schema, table=t["name"])
                    for t in pg.list_tables(conn, schema) if t["kind"] in ("table", "partitioned table")]
    if kind == "table":
        place = {"db": db, "schema": schema, "table": table}
        return [make_node("Columns", "columns", **place), make_node("Constraints", "constraints", **place),
                make_node("Indexes", "indexes", **place)]
    place = {"db": db, "schema": schema, "table": table}
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
