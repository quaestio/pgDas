from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods

from audit.models import record
from servers.models import ServerConnection

from . import pg, tree

# Which tab a node opens on when it is selected in the tree.
TABLE_NODE_TABS = {"table": "properties", "columns": "columns", "column": "columns",
                   "constraints": "constraints", "constraint": "constraints",
                   "indexes": "indexes", "index": "indexes"}
TABS = [("properties", "Properties"), ("columns", "Columns"), ("constraints", "Constraints"),
        ("indexes", "Indexes"), ("data", "Data"), ("sql", "SQL")]


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


def _table_context(conn, node, request):
    db, schema, table = node["db"], node["schema"], node["table"]
    tab = request.GET.get("tab") or TABLE_NODE_TABS.get(node["type"], "properties")
    if tab not in dict(TABS):
        tab = "properties"
    context = {"tab": tab, "tabs": TABS, "title": f"{schema}.{table}", "db": db,
               "base": urlencode({"type": "table", "server": node["server"], "db": db, "schema": schema, "table": table})}
    if tab == "properties":
        context["info"] = pg.table_info(conn, schema, table)
    elif tab == "columns":
        context["columns"] = pg.table_columns(conn, schema, table)
    elif tab == "constraints":
        context["constraints"] = pg.table_constraints(conn, schema, table)
    elif tab == "indexes":
        context["indexes"] = pg.table_indexes(conn, schema, table)
    elif tab == "data":
        pg.table_info(conn, schema, table)  # not found -> PgError, before any SELECT is built
        context["data"] = pg.fetch_rows(conn, schema, table, request.GET.get("page"))
    elif tab == "sql":
        context["ddl"] = pg.table_ddl(conn, schema, table)
    return context


@login_required
@require_GET
def detail(request):
    """HTML fragment for the right-hand pane."""
    node = tree.parse_node(request.GET)
    kind, db, schema = node["type"], node["db"], node["schema"]
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
        elif kind in ("schemas", "schema", "tables"):
            with pg.connect(server, db) as conn:
                context["schemas"] = pg.schema_rows(conn)
                if kind != "schemas":
                    context["owner"] = next((s["owner"] for s in context["schemas"] if s["name"] == schema), "")
                    context["tables"] = [t for t in pg.list_tables(conn, schema) if t["kind"] in ("table", "partitioned table")]
        elif kind in TABLE_NODE_TABS:
            with pg.connect(server, db) as conn:
                context.update(_table_context(conn, node, request))
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
