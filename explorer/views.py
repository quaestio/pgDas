import json
import re
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from audit.models import AuditEntry, record
from servers.models import ServerConnection
from access_control import runtime
from access_control.policy import require, permits, filter_databases, refusal_message
from access_control.sql_policy import authorize
from django.core.exceptions import PermissionDenied

from . import changes, exporter, pg, row_edits, server_admin, table_rebuild, tree

TAB_LABELS = {"properties": "Properties", "partitions": "Partitions", "columns": "Columns", "constraints": "Constraints", "indexes": "Indexes",
              "triggers": "Triggers", "data": "Data", "sql": "Create statement"}
TABLE_TABS = ["properties", "columns", "constraints", "indexes", "triggers", "data", "sql"]
# Which tabs each kind of relation offers.
TABS_BY_KIND = {"table": TABLE_TABS, "partitioned table": ["properties", "partitions"] + TABLE_TABS[1:],
                "view": ["properties", "columns", "data", "sql"],
                "materialized view": ["properties", "columns", "indexes", "data", "sql"],
                "sequence": ["properties", "sql"]}
NODE_TYPE_OF_KIND = {"table": "table", "partitioned table": "table", "view": "view",
                     "materialized view": "matview", "sequence": "sequence"}
RELATION_NODES = {"table", "view", "matview", "sequence"}
EXPORT_CHOICES = [("csv", "CSV"), ("json", "JSON"), ("sql", "SQL")]      # the formats of the Export menu on a Tables list
# The folder or item under a relation selects the matching tab of that relation.
SUBNODE_TABS = {"partitions": "partitions", "columns": "columns", "column": "columns", "constraints": "constraints", "constraint": "constraints",
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
    elif tab == "partitions":
        context["partitions"] = pg.table_partitions(conn, schema, name)
        context["partition_json"] = json.dumps({"strategy": info["strategy"], "key": info["partition_key"]})
    elif tab == "columns":
        context["columns"] = pg.table_columns(conn, schema, name)
        context["column_names"] = json.dumps([c["name"] for c in context["columns"]])
    elif tab == "constraints":
        context["constraints"] = pg.table_constraints(conn, schema, name)
        context["column_names"] = json.dumps([c["name"] for c in pg.table_columns(conn, schema, name)])
    elif tab == "indexes":
        context["indexes"] = pg.table_indexes(conn, schema, name)
        context["column_names"] = json.dumps([c["name"] for c in pg.table_columns(conn, schema, name)])
    elif tab == "triggers":
        context["triggers"] = pg.table_triggers(conn, schema, name)
    elif tab == "data":
        editable = info["relkind"] == "r" and _can_rows(request.user, tree.get_server(node), node["db"])
        context["data"] = pg.fetch_rows(conn, schema, name, request.GET.get("page"), raw=editable)
        if editable:
            pk = changes._primary_key(conn, schema, name)
            data = context["data"]
            if pk:
                names = [c["name"] for c in data["columns"]]
                data["grid"] = [{"cells": [{"shown": shown, "raw": raw} for shown, raw in zip(row, raw_row)],
                                 "key": json.dumps({c: raw_row[names.index(c)] for c in pk})} for row, raw_row in zip(data["rows"], data["raw"])]
                data["grid_json"] = json.dumps({"schema": schema, "table": name, "pk": pk, "columns": [
                    {"name": c["name"], "type": c["type"], "locked": bool(c["generated"] or c["identity"] == "a"), "key": c["name"] in pk,
                     "nullable": c["nullable"], "default": bool(c["default"] or c["identity"])} for c in data["columns"]]})
            else:
                data["no_key"] = True
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
    context = {"tab": tab, "tabs": [("properties", "Properties"), ("sql", "Create statement")], "title": name, "db": node["db"],
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
    return {"tab": tab, "tabs": [("properties", "Properties"), ("sql", "Create statement")], "title": f"{node['schema']}.{info['name']}",
            "db": node["db"], "function": info, "ddl": info["ddl"],
            "base": _base(node, type=node["type"], oid=node["oid"])}


def _type_context(conn, node, request):
    schema, name = node["schema"], node["name"]
    info = pg.type_info(conn, schema, name)
    tab = request.GET.get("tab") if request.GET.get("tab") in ("properties", "sql") else "properties"
    context = {"tab": tab, "tabs": [("properties", "Properties"), ("sql", "Create statement")], "title": f"{schema}.{name}",
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
        context["servers"] = runtime.servers()
        return render(request, "explorer/_detail.html", context)
    server = tree.get_server(node)
    if server is None:
        context["error"] = "That server is not saved in PGDAS."
        return render(request, "explorer/_detail.html", context)
    context["server"] = server
    if kind in {"roles", "role", "tablespaces", "tablespace"}:
        require(request.user, "postgres_roles_manage", server=server)
    elif kind not in {"server", "databases"}:
        require(request.user, "read", server=server, database=db)
    try:
        if kind == "server":
            with pg.connect(server) as conn:
                context["summary"] = pg.server_summary(conn)
                context["server_stats"] = pg.server_stats(conn)
        elif kind == "databases":
            context["can_admin"] = _can_admin(request.user, server)
            with pg.connect(server) as conn:
                context["databases"] = filter_databases(request.user, server, pg.list_databases(conn))
                if context["can_admin"]:
                    context["admin_json"] = _admin_json(server, pg.list_roles(conn), context["databases"])
        elif kind == "database":
            with pg.connect(server) as conn:
                context["database"] = next((d for d in filter_databases(request.user, server, pg.list_databases(conn)) if d["name"] == db), None)
            if context["database"] is None:
                raise pg.PgError(f"Database {db} was not found or you cannot connect to it.")
        elif kind in OBJECT_LISTS:
            context["folder"] = OBJECT_LISTS[kind]
            # Roles and tablespaces belong to the whole server (any database will do); the rest are per database.
            with pg.connect(server, db or None) as conn:
                context["objects"] = _object_list(conn, kind, system)
                if kind == "roles" and _can_admin(request.user, server):
                    context["can_admin"] = True
                    context["admin_json"] = _admin_json(server, pg.list_roles(conn, True), [])
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
                    context["items"] = [t for t in pg.list_tables(conn, schema, system, stats=True) if t["kind"] in FOLDERS["tables"][1]]
                    context["stats"], context["summary"] = True, pg.tables_summary(context["items"], conn)
                    context["can_export"], context["export_formats"] = _can_export(request.user, server, db), EXPORT_CHOICES
                    if kind == "schema" and _can_structure(request.user, server, db):
                        context["structure"] = _structure_json(server, db, schema)
        elif kind in FOLDERS:
            context["folder"], kinds = FOLDERS[kind]
            with pg.connect(server, db) as conn:
                with_stats = kind in ("tables", "matviews")
                context["items"] = [t for t in pg.list_tables(conn, schema, system, stats=with_stats) if t["kind"] in kinds]
                if with_stats:
                    context["stats"], context["summary"] = True, pg.tables_summary(context["items"], conn)
            context["can_export"], context["export_formats"] = _can_export(request.user, server, db), EXPORT_CHOICES
            if kind == "tables" and _can_structure(request.user, server, db):
                context["structure"] = _structure_json(server, db, schema)
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
            context["can_export"] = context.get("tab") == "data" and _can_export(request.user, server, db)
            if context.get("relation_kind") in ("table", "partitioned table") and _can_structure(request.user, server, db):
                context["structure"] = _structure_json(server, db, schema, node["table"])
            if context.get("relation_kind") in ("table", "partitioned table", "materialized view") and permits(request.user, "maintenance", server=server, database=db):
                context["maintenance"] = json.dumps({"url": reverse("maintenance_run", args=[server.pk]), "db": db, "schema": schema, "table": node["table"]})
        elif kind in ("function", "procedure"):
            with pg.connect(server, db) as conn:
                context.update(_function_context(conn, node, request))
        elif kind == "type":
            with pg.connect(server, db) as conn:
                context.update(_type_context(conn, node, request))
    except pg.PgError as error:
        context["error"] = str(error)
    return render(request, "explorer/_detail.html", context)


def _can_write(user, server):
    """Staff users can change anything on any saved server. Every change still goes through its popup and the audit log."""
    return runtime.is_admin(user)


_can_admin = _can_write


def _can_structure(user, server, database=None):
    """Super Users anywhere; anyone else only on a database where their access lets them create, alter or drop (the policy checks each
    statement again when it is sent)."""
    if runtime.is_admin(user):
        return True
    return bool(server is not None and database and any(permits(user, action, server=server, database=database)
                                                        for action in ("create", "alter", "drop", "views", "sequences", "schemas")))


def _can_rows(user, server, database=None):
    """Whether the data grid can be edited: Super Users, or someone who may insert, update or delete in that database."""
    if runtime.is_admin(user):
        return True
    return bool(server is not None and database and any(permits(user, action, server=server, database=database)
                                                        for action in ("insert", "update", "delete")))


def _admin_json(server, roles, databases):
    """What the Create database / Create role / Rename dialogs need to fill their lists, as one JSON attribute."""
    return json.dumps({"server": server.pk, "roles": [r["name"] for r in roles], "databases": [d["name"] for d in databases]})


def _structure_json(server, db, schema, table=""):
    """Where the table-structure buttons (New table, Rename, Drop, column actions) apply, as one JSON attribute."""
    return json.dumps({"server": server.pk, "db": db, "schema": schema, "table": table,
                       "types_url": reverse("column_types", args=[server.pk]) + "?" + urlencode({"db": db}),
                       "tables_url": reverse("database_tables", args=[server.pk]) + "?" + urlencode({"db": db})})


@login_required
@require_POST
def maintenance_run(request, pk):
    """VACUUM / ANALYZE / REINDEX of one table, for Super Users and for people with the Maintenance permission on that database."""
    server = get_object_or_404(ServerConnection, pk=pk)
    try:
        data = json.loads(request.body or b"{}")
        database, schema, table, operation = (str(data.get(key) or "") for key in ("db", "schema", "table", "operation"))
        if not (database and schema and table):
            raise ValueError
    except (ValueError, AttributeError):
        return JsonResponse({"ok": False, "error": "That request was not understood."}, status=400)
    require(request.user, "maintenance", server=server, database=database)
    request.pgdas_database = database
    try:
        message = pg.run_maintenance(server, database, schema, table, operation)
    except pg.PgError as error:
        record(request.user, f"maintenance.{operation}", server, f"{schema}.{table}", ok=False, payload={"database": database})
        return JsonResponse({"ok": False, "error": str(error)}, status=400)
    record(request.user, f"maintenance.{operation}", server, f"{schema}.{table}", ok=True, payload={"database": database})
    return JsonResponse({"ok": True, "message": message})


@login_required
@require_GET
def database_tables(request, pk):
    """The tables (with their columns) of one database, for the foreign key picker of the constraint dialogs."""
    server = get_object_or_404(runtime.servers(), pk=pk)
    database = request.GET.get("db") or ""
    require(request.user, "read", server=server, database=database)
    try:
        with pg.connect(server, database or None) as conn:
            found = pg.database_tables(conn, with_views=request.GET.get("views") == "1")
    except pg.PgError as error:
        return JsonResponse({"error": str(error)}, status=502)
    return JsonResponse({"tables": found})


@login_required
@require_GET
def column_types(request, pk):
    """The enums, domains, composite / range types and extension types of one database, for the column type lists."""
    server = get_object_or_404(runtime.servers(), pk=pk)
    database = request.GET.get("db") or ""
    require(request.user, "read", server=server, database=database)
    try:
        with pg.connect(server, database or None) as conn:
            found = pg.column_types(conn)
            roles = [r["name"] for r in pg.list_roles(conn)]                      # for the owner and privileges of a new table
            spaces = [t["name"] for t in pg.list_tablespaces(conn) if t["name"] != "pg_global"]    # and its tablespace (pg_global holds only shared catalogs)
    except pg.PgError as error:
        return JsonResponse({"error": str(error)}, status=502)
    return JsonResponse({"types": [{"name": name, "kind": kind} for name, kind in found], "roles": roles, "tablespaces": spaces})


def _refuse(message, status=403):
    return JsonResponse({"ok": False, "error": message}, status=status)


def _write_request(request, pk):
    """Common checks for the endpoints that change data: returns (server, kind, data, None) or (None, None, None, error response)."""
    server = get_object_or_404(ServerConnection, pk=pk)
    try:
        data = json.loads(request.body or b"{}")
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        return None, None, None, _refuse("That request was not understood.", 400)
    try:
        kind = changes.classify(str(data.get("sql") or ""))
        database = str(data.get("db") or "")
        if not database and runtime.is_admin():
            database = server.database_name
        if table_rebuild.is_reorder_statement(str(data.get("sql") or "")):
            # PGDAS's own statement (not PostgreSQL SQL, so it cannot be parsed for the policy): a rebuild is for Super Users only.
            require(request.user, "read", server=server, database=database)
            # The table is dropped and created again with its rows put back, so it takes every one of these on that database.
            for action in ("create", "alter", "drop", "insert"):
                require(request.user, action, server=server, database=database)
        elif kind == "create" and changes.table_script_statements(str(data.get("sql") or "")):
            # A new table with its owner, comments and privileges: several statements, so the one-statement policy parser cannot read them.
            # Setting an owner and privileges is for Super Users; the engine accepts only CREATE TABLE plus those statements for that table.
            require(request.user, "read", server=server, database=database)
            if not runtime.is_admin(request.user):
                raise PermissionDenied("Setting an owner, comments or privileges while creating a table requires a Super User.")
        else:
            authorize(request.user, server, database, str(data.get("sql") or ""), write=True)
        request.pgdas_database = database
        data["db"] = database
    except pg.PgError as error:
        return None, None, None, _refuse(str(error), 400)
    return server, kind, data, None


@login_required
@require_POST
def write_preview(request, pk):
    """Step 1 of UPDATE, DELETE, TRUNCATE, DROP, CREATE and ALTER: what the statement would do. Nothing is changed (always rolled back)."""
    server, kind, data, failure = _write_request(request, pk)
    if failure:
        return failure
    try:
        preview = changes.preview(server, str(data.get("db") or ""), str(data.get("sql") or ""))
    except pg.PgError as error:
        return _refuse(str(error), 400)
    return JsonResponse({"ok": True, **preview}, json_dumps_params={"default": str})


@login_required
@require_POST
def write_execute(request, pk):
    """Step 2 (the only step for INSERT): run it for real. The word typed in the popup and the facts shown are checked again."""
    server, kind, data, failure = _write_request(request, pk)
    if failure:
        return failure
    try:
        result = changes.execute(server, str(data.get("db") or ""), str(data.get("sql") or ""),
                                 data.get("fingerprint"), data.get("confirm"), request.user)
    except pg.PgError as error:
        record(request.user, kind, server, str(data.get("sql") or "")[:2000], ok=False)    # a refused attempt is logged too
        return _refuse(str(error), 400)
    return JsonResponse({"ok": True, **result}, json_dumps_params={"default": str})


def _rows_request(request, pk):
    """Common checks for the data grid's save: (server, data, None) or (None, None, error response). Only Super Users edit in the grid."""
    server = get_object_or_404(ServerConnection, pk=pk)
    try:
        data = json.loads(request.body or b"{}")
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        return None, None, _refuse("That request was not understood.", 400)
    database = str(data.get("db") or "") or server.database_name
    try:
        require(request.user, "read", server=server, database=database)
        if not _can_rows(request.user, server, database):
            raise PermissionDenied(refusal_message(request.user, "update", server=server, database=database))
    except PermissionDenied as error:
        return None, None, _refuse(str(error) or "Not allowed.")
    request.pgdas_database = database
    data["db"] = database
    return server, data, None


@login_required
@require_POST
def rows_preview(request, pk):
    """Step 1 of saving the grid's edits: what the whole batch would do. Nothing is changed (always rolled back)."""
    server, data, failure = _rows_request(request, pk)
    if failure:
        return failure
    try:
        preview = row_edits.preview(server, str(data.get("db") or ""), data)
    except pg.PgError as error:
        return _refuse(str(error), 400)
    return JsonResponse({"ok": True, **preview}, json_dumps_params={"default": str})


@login_required
@require_POST
def rows_execute(request, pk):
    """Step 2: save the batch for real, after the checked facts and the typed word are verified again."""
    server, data, failure = _rows_request(request, pk)
    if failure:
        return failure
    try:
        result = row_edits.execute(server, str(data.get("db") or ""), data, data.get("fingerprint") if isinstance(data.get("fingerprint"), dict) else {},
                                   data.get("confirm"), request.user)
    except pg.PgError as error:
        record(request.user, "rows.edit", server, f"Saving edits to {data.get('schema')}.{data.get('table')} failed: {error}"[:2000], ok=False)
        return _refuse(str(error), 400)
    return JsonResponse({"ok": True, **result}, json_dumps_params={"default": str})


def _can_export(user, server, database):
    """Whoever the access rules let export from this database may download its tables."""
    return permits(user, "export", server=server, database=database)


@login_required
@require_GET
def table_export(request, pk):
    """Download a table or view as CSV, JSON or SQL INSERT statements (at most MAX_EXPORT_ROWS rows), read-only and recorded."""
    server = get_object_or_404(runtime.servers(), pk=pk)
    database, schema, table = request.GET.get("db", ""), request.GET.get("schema", ""), request.GET.get("table", "")
    fmt = request.GET.get("format", "csv")
    if fmt not in exporter.FORMATS or not (schema and table):
        return HttpResponse("That export was not understood.", status=400, content_type="text/plain")
    if not _can_export(request.user, server, database):
        return HttpResponse("You are not allowed to export from this database.", status=403, content_type="text/plain")
    request.pgdas_database = database
    limit = int(settings.MAX_EXPORT_ROWS)
    try:
        conn_manager = pg.connect(server, database or None)
        conn = conn_manager.__enter__()
    except pg.PgError as error:
        return HttpResponse(str(error), status=502, content_type="text/plain")
    try:
        info = pg.relation_info(conn, schema, table, kinds=("r", "p", "v", "m"))
        columns = [c for c in pg.table_columns(conn, schema, table)]
        names = [c["name"] for c in columns]
        ident = lambda n: '"' + n.replace('"', '""') + '"'
        source = f"{ident(schema)}.{ident(table)}"
        select = ", ".join((f"t.{ident(n)}" if fmt == "json" else f"t.{ident(n)}::text") for n in names)
        total = conn.execute(f"SELECT count(*) FROM (SELECT 1 FROM {source} LIMIT {limit}) s").fetchone()[0]
    except pg.PgError as error:
        conn_manager.__exit__(None, None, None)
        return HttpResponse(str(error), status=404, content_type="text/plain")
    record(request.user, "table.export", server, f"Exported {schema}.{table} as {fmt.upper()} ({info['kind']})", rows=total,
           payload={"database": database, "schema": schema, "table": table, "format": fmt, "count": total})

    def rows():
        try:
            with conn.cursor(name="pgdas_export") as cursor:
                cursor.itersize = 1000
                cursor.execute(f"SELECT {select} FROM {source} t LIMIT {limit}")
                yield from cursor
        finally:
            conn_manager.__exit__(None, None, None)

    content_type, extension = exporter.FORMATS[fmt]
    response = StreamingHttpResponse(exporter.stream(fmt, names, rows(), (schema, table), [c["type"] for c in columns]), content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{table}.{extension}"'
    return response


@login_required
@require_http_methods(["GET", "POST"])
def sql_runner(request, pk):
    server = get_object_or_404(ServerConnection, pk=pk)
    db = request.GET.get("db") or request.POST.get("db") or ""
    db = db or runtime.default_database(server) or server.database_name
    require(request.user, "read", server=server, database=db)
    request.pgdas_database = db
    context = {"server": server, "sql": "", "db": db, "can_write": _can_write(request.user, server),
               "can_structure": _can_structure(request.user, server, db), "can_admin": _can_admin(request.user, server),
               "confirm_words": changes.CONFIRM_WORDS}
    context["can_write"] = any(permits(request.user, action, server=server, database=db) for action in ("insert", "update", "delete", "truncate", "create", "alter", "drop"))
    context["can_export"] = permits(request.user, "export", server=server, database=db)
    refused = False                                                         # a statement the permissions refuse: the page answers 403 and says why
    if request.method == "POST":
        statement = request.POST.get("sql", "").strip()
        context["sql"] = statement
        if statement:
            result, problem = _run_statement(request, server, db, statement)
            if problem:
                refused = problem["refused"]
                context["error"] = problem["error"]
                if problem["hint"]:
                    context["hint"] = problem["hint"]
            else:
                context["result"] = result
    context["history"] = _history(request.user, server)
    context["databases"] = _runner_databases(request.user, server, db)
    return render(request, "explorer/sql.html", context, status=403 if refused else 200)


def _run_statement(request, server, db, statement):
    """Run a statement the SQL runner way: checked against this person's permissions, read-only, recorded in the audit log either way.
    Returns (result, None) or (None, {"error", "hint", "refused"}); a refused statement is answered with the reason."""
    try:
        authorize(request.user, server, db, statement)
        result = pg.run_sql(server, statement, db or None)
    except (pg.PgError, PermissionDenied) as error:
        refused = isinstance(error, PermissionDenied)
        blocked_by = "pgdas" if refused else "postgresql" if "PostgreSQL blocked this" in str(error) else "error"          # which gate stopped it
        record(request.user, "sql.run", server, statement, ok=False, payload={"database": db, "blocked_by": blocked_by})
        hint = ""
        verb = re.match(r"\s*(insert|update|delete|truncate|drop|create|alter|grant|revoke)\b", statement, re.I)
        if verb and ("read-only transaction" in str(error) or refused):
            if not request.user.is_staff:
                hint = "Only staff users can change data."
            else:
                hint = f"{verb.group(1).upper()} asks for confirmation first, and that popup needs JavaScript. Turn it on and press Run again."
        return None, {"error": str(error) or "You are not permitted to run that statement.", "hint": hint, "refused": refused}
    record(request.user, "sql.run", server, statement, ok=True, rows=result["count"])
    return result, None


RESULT_PAGE_SIZE = 10                       # rows shown at a time; the rest of the (capped) result is paged through
RESULT_KEEP_SECONDS = 600


def _result_page(result, token, page):
    """One page of a query result, as the SQL runner's script wants it."""
    total = result["count"]
    pages = max(1, -(-total // RESULT_PAGE_SIZE))
    page = min(max(int(page or 1), 1), pages)
    start = (page - 1) * RESULT_PAGE_SIZE
    return {"ok": True, "columns": result["columns"], "rows": result["rows"][start:start + RESULT_PAGE_SIZE], "message": result["message"], "ms": result["ms"],
            "total": total, "truncated": result["truncated"], "page": page, "pages": pages, "page_size": RESULT_PAGE_SIZE, "token": token,
            "first": start + 1 if total else 0, "last": min(start + RESULT_PAGE_SIZE, total)}


def _sql_body(request, pk):
    """(server, database, statement, body) of an SQL runner request sent as JSON, with the same permission check as the page itself."""
    server = get_object_or_404(ServerConnection, pk=pk)
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        body = {}
    db = str(body.get("db") or "") or runtime.default_database(server) or server.database_name
    require(request.user, "read", server=server, database=db)
    request.pgdas_database = db
    return server, db, str(body.get("sql") or "").strip(), body


def _keep_result(request, server, db, result):
    import secrets
    from django.core.cache import cache
    token = secrets.token_urlsafe(18)
    cache.set(f"pgdas-sqlres:{token}", {"user": request.user.pk, "server": server.pk, "db": db, "result": result}, RESULT_KEEP_SECONDS)
    return token


@login_required
@require_POST
def sql_run(request, pk):
    """Run a statement without leaving the page: the first page of the result as JSON, the rest kept for a few minutes to page through."""
    server, db, statement, _ = _sql_body(request, pk)
    if not statement:
        return JsonResponse({"ok": False, "error": "Write a statement first.", "hint": ""}, status=400)
    result, problem = _run_statement(request, server, db, statement)
    if problem:
        return JsonResponse({"ok": False, **problem}, status=403 if problem["refused"] else 400)
    return JsonResponse(_result_page(result, _keep_result(request, server, db, result) if result["columns"] else "", 1))


@login_required
@require_POST
def sql_page(request, pk):
    """Another page of a result that was just run. If it has been forgotten (a restart, ten minutes) the statement is run again."""
    from django.core.cache import cache
    server, db, statement, body = _sql_body(request, pk)
    token = str(body.get("token") or "")
    kept = cache.get(f"pgdas-sqlres:{token}") if token else None
    if kept and (kept["user"], kept["server"], kept["db"]) == (request.user.pk, server.pk, db):
        return JsonResponse(_result_page(kept["result"], token, body.get("page")))
    if not statement:
        return JsonResponse({"ok": False, "error": "That result is no longer available. Run the statement again.", "hint": ""}, status=400)
    try:
        authorize(request.user, server, db, statement)
        result = pg.run_sql(server, statement, db or None)
    except (pg.PgError, PermissionDenied) as error:
        return JsonResponse({"ok": False, "error": str(error) or "You are not permitted to run that statement.", "hint": ""}, status=400)
    return JsonResponse(_result_page(result, _keep_result(request, server, db, result) if result["columns"] else "", body.get("page")))


def _runner_databases(user, server, current):
    """The databases to pick from in the SQL runner: those this person may connect to, always including the one in use."""
    try:
        with pg.connect(server) as conn:
            names = [d["name"] for d in filter_databases(user, server, pg.list_databases(conn))]
    except pg.PgError:
        names = []
    return names if current in names else ([current] if current else []) + names


HISTORY_ACTIONS = ("sql.run", "insert", "update", "delete", "truncate", "drop", "create", "alter", "grant", "revoke")


def _history(user, server, limit=20):
    """The signed-in person's own latest statements on this server, newest first, each statement once."""
    seen, found = set(), []
    for entry in runtime.audits().filter(user=user, server=server, action__in=HISTORY_ACTIONS).order_by("-created_at", "-pk")[:300]:
        key = " ".join(entry.detail.split())
        if key and key not in seen:
            seen.add(key)
            found.append(entry)
            if len(found) == limit:
                break
    return found


_EXPORT_FROM = re.compile(r'^\s*FROM\s+(?:ONLY\s+)?(?P<first>"(?:[^"]|"")+"|[A-Za-z_][\w$]*)(?:\s*\.\s*(?P<second>"(?:[^"]|"")+"|[A-Za-z_][\w$]*))?', re.I)


def _export_table(statement):
    """(schema, table) the INSERT statements of a query export are written for: the table of a simple SELECT ... FROM table, else public.query_result."""
    plain = re.sub(r"'(?:[^']|'')*'", lambda m: "'" + "x" * (len(m.group()) - 2) + "'", re.sub(r"--[^\n]*|/\*.*?\*/", " ", statement, flags=re.S))
    # The same text with the insides of quoted names blanked out (same length), so a table called "Order Lines" is not read as ORDER.
    masked = re.sub(r'"(?:[^"]|"")+"', lambda m: '"' + "x" * (len(m.group()) - 2) + '"', plain)
    unquote = lambda name: name[1:-1].replace('""', '"') if name.startswith('"') else name.lower()
    froms = list(re.finditer(r"\bFROM\b", masked, re.I))
    if len(froms) == 1 and len(re.findall(r"\bSELECT\b", masked, re.I)) == 1 and not re.search(r"\b(UNION|INTERSECT|EXCEPT)\b", masked, re.I):
        tail, tail_masked = plain[froms[0].end():], masked[froms[0].end():]
        clause = re.search(r"\b(?:WHERE|GROUP|ORDER|LIMIT|OFFSET|HAVING|FETCH)\b", tail_masked, re.I)       # what follows FROM, up to the next clause
        cut = clause.start() if clause else len(tail)
        source, source_masked = tail[:cut], tail_masked[:cut]
        match = _EXPORT_FROM.match("FROM " + source)
        if match and "," not in source_masked and not re.search(r"\bJOIN\b|\(", source_masked, re.I):
            first, second = unquote(match["first"]), match["second"] and unquote(match["second"])
            return (first, second) if second else ("public", first)
    return ("public", "query_result")


@login_required
@require_POST
def sql_export(request, pk):
    import csv
    from django.http import HttpResponse
    server = get_object_or_404(runtime.servers(), pk=pk)
    database = request.POST.get("db", "")
    require(request.user, "export", server=server, database=database)
    statement = request.POST.get("sql", "")
    authorize(request.user, server, database, statement)
    request.pgdas_database = database
    result = pg.run_sql(server, statement, database)
    fmt = request.POST.get("format", "csv")
    if fmt not in ("csv", "json", "sql"):
        fmt = "csv"
    content_type, extension = exporter.FORMATS[fmt]
    table = _export_table(statement) if fmt == "sql" else None
    response = HttpResponse("".join(exporter.stream(fmt, result["columns"], result["rows"], table=table)), content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="pgdas-query.{extension}"'
    record(request.user, "sql.export", server, statement, rows=result["count"])
    return response
