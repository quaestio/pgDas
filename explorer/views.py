from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods

from audit.models import record
from servers.models import ServerConnection

from . import pg, tree

TAB_LABELS = {"properties": "Properties", "columns": "Columns", "constraints": "Constraints", "indexes": "Indexes",
              "triggers": "Triggers", "data": "Data", "sql": "SQL"}
TABLE_TABS = ["properties", "columns", "constraints", "indexes", "triggers", "data", "sql"]
# Which tabs each kind of relation offers.
TABS_BY_KIND = {"table": TABLE_TABS, "partitioned table": TABLE_TABS,
                "view": ["properties", "columns", "data", "sql"],
                "materialized view": ["properties", "columns", "indexes", "data", "sql"],
                "sequence": ["properties", "sql"]}
NODE_TYPE_OF_KIND = {"table": "table", "partitioned table": "table", "view": "view",
                     "materialized view": "matview", "sequence": "sequence"}
RELATION_NODES = {"table", "view", "matview", "sequence"}
# The folder or item under a relation selects the matching tab of that relation.
SUBNODE_TABS = {"columns": "columns", "column": "columns", "constraints": "constraints", "constraint": "constraints",
                "indexes": "indexes", "index": "indexes", "triggers": "triggers", "trigger": "triggers"}
FOLDERS = {"tables": ("Tables", ("table", "partitioned table")), "views": ("Views", ("view",)),
           "matviews": ("Materialized Views", ("materialized view",)), "sequences": ("Sequences", ("sequence",))}


@login_required
def explorer(request, pk):
    """Old per-server address: the single Object Browser opens with that server selected."""
    get_object_or_404(ServerConnection, pk=pk)
    return redirect(f"{reverse('server_list')}?server={pk}")


@login_required
@require_GET
def tree_nodes(request):
    try:
        return JsonResponse({"nodes": tree.children(tree.parse_node(request.GET))})
    except pg.PgError as error:
        return JsonResponse({"error": str(error)}, status=502)


def _base(node, **identity):
    """Query string that re-opens the same object; the tab and page links append to it."""
    params = {"server": node["server"], "db": node["db"], "schema": node["schema"], **identity}
    if node["system"]:
        params["system"] = "1"
    return urlencode(params)


def _pick_tab(request, node, tabs):
    wanted = request.GET.get("tab") or SUBNODE_TABS.get(node["type"], "properties")
    return wanted if wanted in tabs else "properties"


def _relation_context(conn, node, request):
    schema, name = node["schema"], node["table"]
    info = pg.relation_info(conn, schema, name)
    tabs = TABS_BY_KIND[info["kind"]]
    tab = _pick_tab(request, node, tabs)
    context = {"tab": tab, "tabs": [(key, TAB_LABELS[key]) for key in tabs], "title": f"{schema}.{name}", "db": node["db"],
               "info": info, "relation_kind": info["kind"],
               "base": _base(node, type=NODE_TYPE_OF_KIND[info["kind"]], table=name)}
    if tab == "properties" and info["relkind"] == "S":
        context["sequence"] = pg.sequence_info(conn, schema, name)
    elif tab == "columns":
        context["columns"] = pg.table_columns(conn, schema, name)
    elif tab == "constraints":
        context["constraints"] = pg.table_constraints(conn, schema, name)
    elif tab == "indexes":
        context["indexes"] = pg.table_indexes(conn, schema, name)
    elif tab == "triggers":
        context["triggers"] = pg.table_triggers(conn, schema, name)
    elif tab == "data":
        context["data"] = pg.fetch_rows(conn, schema, name, request.GET.get("page"))
    elif tab == "sql":
        if info["relkind"] in ("v", "m"):
            context["ddl"] = pg.view_ddl(conn, schema, name)
        elif info["relkind"] == "S":
            context["ddl"] = pg.sequence_ddl(conn, schema, name)
        else:
            context["ddl"] = pg.table_ddl(conn, schema, name)
    return context


# Single objects with a Properties tab and an SQL tab: (look-up function, CREATE statement builder).
OBJECTS = {"role": (pg.role_info, pg.role_ddl), "tablespace": (pg.tablespace_info, pg.tablespace_ddl),
           "extension": (pg.extension_info, pg.extension_ddl), "eventtrigger": (pg.event_trigger_info, pg.event_trigger_ddl),
           "publication": (pg.publication_info, pg.publication_ddl)}
# Folders that list such objects (server level: roles, tablespaces; database level: the rest).
OBJECT_LISTS = {"roles": "Roles", "tablespaces": "Tablespaces", "catalogs": "Catalogs", "extensions": "Extensions",
                "eventtriggers": "Event Triggers", "publications": "Publications"}


def _object_context(conn, node, request):
    kind, name = node["type"], node["name"]
    info_of, ddl_of = OBJECTS[kind]
    data = info_of(conn, name)
    tab = request.GET.get("tab") if request.GET.get("tab") in ("properties", "sql") else "properties"
    context = {"tab": tab, "tabs": [("properties", "Properties"), ("sql", "SQL")], "title": name, "db": node["db"],
               "object_kind": kind, "object": data, "base": _base(node, type=kind, name=name)}
    if tab == "sql":
        context["ddl"] = ddl_of(conn, name)
    return context


def _object_list(conn, kind, system):
    return {"roles": lambda: pg.list_roles(conn, system), "tablespaces": lambda: pg.list_tablespaces(conn),
            "catalogs": lambda: pg.catalog_rows(conn), "extensions": lambda: pg.list_extensions(conn),
            "eventtriggers": lambda: pg.list_event_triggers(conn), "publications": lambda: pg.list_publications(conn)}[kind]()


def _function_context(conn, node, request):
    info = pg.function_info(conn, node["schema"], node["oid"])
    tab = request.GET.get("tab") if request.GET.get("tab") in ("properties", "sql") else "properties"
    return {"tab": tab, "tabs": [("properties", "Properties"), ("sql", "SQL")], "title": f"{node['schema']}.{info['name']}",
            "db": node["db"], "function": info, "ddl": info["ddl"],
            "base": _base(node, type=node["type"], oid=node["oid"])}


def _type_context(conn, node, request):
    schema, name = node["schema"], node["name"]
    info = pg.type_info(conn, schema, name)
    tab = request.GET.get("tab") if request.GET.get("tab") in ("properties", "sql") else "properties"
    context = {"tab": tab, "tabs": [("properties", "Properties"), ("sql", "SQL")], "title": f"{schema}.{name}",
               "db": node["db"], "type_info": info, "base": _base(node, type="type", name=name)}
    if tab == "sql":
        context["ddl"] = pg.type_ddl(conn, schema, name)
    return context


@login_required
@require_GET
def detail(request):
    """HTML fragment for the main pane."""
    node = tree.parse_node(request.GET)
    kind, db, schema, system = node["type"], node["db"], node["schema"], node["system"]
    context = {"kind": kind, "node": node}
    if kind == "servers":
        context["servers"] = ServerConnection.objects.all()
        return render(request, "explorer/_detail.html", context)
    server = tree.get_server(node)
    if server is None:
        context["error"] = "That server is not saved in PgDeck."
        return render(request, "explorer/_detail.html", context)
    context["server"] = server
    try:
        if kind == "server":
            with pg.connect(server) as conn:
                context["summary"] = pg.server_summary(conn)
        elif kind == "databases":
            with pg.connect(server) as conn:
                context["databases"] = pg.list_databases(conn)
        elif kind == "database":
            with pg.connect(server) as conn:
                context["database"] = next((d for d in pg.list_databases(conn) if d["name"] == db), None)
            if context["database"] is None:
                raise pg.PgError(f"Database {db} was not found or you cannot connect to it.")
        elif kind in OBJECT_LISTS:
            context["folder"] = OBJECT_LISTS[kind]
            # Roles and tablespaces belong to the whole server (any database will do); the rest are per database.
            with pg.connect(server, db or None) as conn:
                context["objects"] = _object_list(conn, kind, system)
        elif kind in OBJECTS:
            with pg.connect(server, node["db"] or None) as conn:
                context.update(_object_context(conn, node, request))
        elif kind in ("schemas", "schema", "catalog"):
            with pg.connect(server, db) as conn:
                context["schemas"] = pg.schema_rows(conn)
                if kind in ("schema", "catalog"):
                    owners = {s["name"]: s["owner"] for s in context["schemas"] + pg.catalog_rows(conn)}
                    context["owner"] = owners.get(schema, "")
                    context["folder"] = "Tables"
                    context["items"] = [t for t in pg.list_tables(conn, schema, system) if t["kind"] in FOLDERS["tables"][1]]
        elif kind in FOLDERS:
            context["folder"], kinds = FOLDERS[kind]
            with pg.connect(server, db) as conn:
                context["items"] = [t for t in pg.list_tables(conn, schema, system) if t["kind"] in kinds]
        elif kind in ("functions", "procedures"):
            context["folder"] = kind.capitalize()
            with pg.connect(server, db) as conn:
                context["functions"] = pg.list_functions(conn, schema, procedures=kind == "procedures", system=system)
        elif kind == "types":
            context["folder"] = "Types / Domains"
            with pg.connect(server, db) as conn:
                context["types"] = pg.list_types(conn, schema, system)
        elif kind in RELATION_NODES or kind in SUBNODE_TABS:
            with pg.connect(server, db) as conn:
                context.update(_relation_context(conn, node, request))
        elif kind in ("function", "procedure"):
            with pg.connect(server, db) as conn:
                context.update(_function_context(conn, node, request))
        elif kind == "type":
            with pg.connect(server, db) as conn:
                context.update(_type_context(conn, node, request))
    except pg.PgError as error:
        context["error"] = str(error)
    return render(request, "explorer/_detail.html", context)


@login_required
@require_http_methods(["GET", "POST"])
def sql_runner(request, pk):
    server = get_object_or_404(ServerConnection, pk=pk)
    db = request.GET.get("db") or request.POST.get("db") or ""
    context = {"server": server, "sql": "", "db": db}
    if request.method == "POST":
        statement = request.POST.get("sql", "").strip()
        context["sql"] = statement
        if statement:
            try:
                result = pg.run_sql(server, statement, db or None)
            except pg.PgError as error:
                record(request.user, "sql.run", server, statement, ok=False)
                context["error"] = str(error)
            else:
                record(request.user, "sql.run", server, statement, ok=True, rows=result["count"])
                context["result"] = result
    return render(request, "explorer/sql.html", context)
