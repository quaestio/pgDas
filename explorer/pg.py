"""PostgreSQL access for PgDeck. Everything here runs in a READ ONLY transaction enforced by the server."""
import json
import re
import time
from contextlib import contextmanager

import psycopg
from django.conf import settings
from psycopg import sql

PAGE_SIZE = 50


class PgError(Exception):
    """A database problem worded for the user; never contains the password."""


def _message(error):
    diag = getattr(error, "diag", None)
    return (getattr(diag, "message_primary", None) or str(error)).strip()


@contextmanager
def connect(server, database=None):
    """Open a read-only connection; `database` browses another database on the same server."""
    try:
        conn = psycopg.connect(
            host=server.host, port=server.port, dbname=database or server.database, user=server.username,
            password=server.get_password(), sslmode=server.sslmode, connect_timeout=10,
            application_name="PgDeck", options=f"-c statement_timeout={int(settings.STATEMENT_TIMEOUT_MS)}",
        )
    except psycopg.Error as error:
        raise PgError(_message(error))
    except ValueError as error:
        raise PgError(str(error))
    try:
        conn.read_only = True  # BEGIN READ ONLY: the database itself rejects any write.
        yield conn
    except psycopg.Error as error:
        raise PgError(_message(error))
    finally:
        conn.rollback()
        conn.close()


def test_connection(server):
    with connect(server) as conn:
        return conn.execute("SHOW server_version").fetchone()[0]


def server_summary(conn):
    version, user, database = conn.execute("SELECT version(), current_user, current_database()").fetchone()
    return {"version": version, "user": user, "database": database}


def list_databases(conn):
    rows = conn.execute(
        """SELECT d.datname, pg_get_userbyid(d.datdba), pg_encoding_to_char(d.encoding), pg_database_size(d.datname)
           FROM pg_database d
           WHERE d.datallowconn AND NOT d.datistemplate AND has_database_privilege(d.datname, 'CONNECT')
           ORDER BY d.datname""").fetchall()
    return [{"name": n, "owner": o, "encoding": e, "size": s, "size_text": size_text(s)} for n, o, e, s in rows]


def size_text(size):
    size = float(size or 0)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


# Objects created by an extension (pg_trgm, PostGIS, ...) are hidden unless "show system objects" is on.
NOT_IN_EXTENSION = """NOT EXISTS (SELECT 1 FROM pg_depend ext WHERE ext.objid = {oid} AND ext.classid = '{catalog}'::regclass
                                     AND ext.deptype = 'e')"""


def schema_rows(conn, system=False):
    where = "nspname <> 'information_schema' AND nspname !~ '^pg_(toast|temp|toast_temp)'" if system else \
        "nspname NOT LIKE 'pg\\_%' AND nspname <> 'information_schema'"
    rows = conn.execute(f"SELECT nspname, pg_get_userbyid(nspowner) FROM pg_namespace WHERE {where} ORDER BY 1").fetchall()
    if system:  # information_schema is a system schema too: show it when asked
        rows = sorted(rows + conn.execute("SELECT nspname, pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'information_schema'").fetchall())
    return [{"name": name, "owner": owner} for name, owner in rows]


def relation_info(conn, schema, name, kinds=("r", "p", "v", "m", "S")):
    """Owner, kind, size and comment of a table, view, materialized view or sequence."""
    row = conn.execute(
        """SELECT pg_get_userbyid(c.relowner), c.relkind::text, CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END,
                  pg_total_relation_size(c.oid), obj_description(c.oid, 'pg_class'), c.relispopulated
           FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = %s AND c.relname = %s AND c.relkind = ANY(%s)""", (schema, name, list(kinds))).fetchone()
    if row is None:
        raise PgError(f"{schema}.{name} was not found.")
    owner, kind, estimate, size, comment, populated = row
    return {"owner": owner, "kind": KINDS[kind], "relkind": kind, "estimate": estimate, "size_text": size_text(size),
            "comment": comment or "", "populated": populated}


def table_info(conn, schema, table):
    try:
        return relation_info(conn, schema, table, kinds=("r", "p"))
    except PgError:
        raise PgError(f"Table {schema}.{table} was not found.")


CONSTRAINT_TYPES = {"p": "PRIMARY KEY", "f": "FOREIGN KEY", "u": "UNIQUE", "c": "CHECK", "x": "EXCLUDE", "t": "TRIGGER"}


def table_constraints(conn, schema, table):
    rows = conn.execute(
        """SELECT k.conname, k.contype, pg_get_constraintdef(k.oid)
           FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = %s AND c.relname = %s
             AND k.contype <> 'n'  -- PostgreSQL 18 records NOT NULL as a constraint; the Columns tab already shows it
           ORDER BY (k.contype = 'p') DESC, k.conname""", (schema, table)).fetchall()
    return [{"name": name, "type": CONSTRAINT_TYPES.get(kind, kind), "definition": definition} for name, kind, definition in rows]


def table_indexes(conn, schema, table):
    rows = conn.execute(
        """SELECT i.relname, pg_get_indexdef(i.oid), x.indisunique, x.indisprimary
           FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid JOIN pg_class c ON c.oid = x.indrelid
           JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = %s AND c.relname = %s
           ORDER BY x.indisprimary DESC, i.relname""", (schema, table)).fetchall()
    return [{"name": name, "definition": definition, "unique": unique, "primary": primary}
            for name, definition, unique, primary in rows]


SERIAL_TYPES = {"smallint": "smallserial", "integer": "serial", "bigint": "bigserial"}


def _quote(conn, name):
    return conn.execute("SELECT quote_ident(%s)", (name,)).fetchone()[0]


def table_ddl(conn, schema, table):
    """A readable CREATE TABLE for a table, rebuilt from the catalogs (PostgreSQL has no built-in for this)."""
    table_info(conn, schema, table)  # raises PgError when missing
    q = lambda name: _quote(conn, name)
    lines = []
    for column in table_columns(conn, schema, table):
        column_type, default = column["type"], column["default"]
        serial = SERIAL_TYPES.get(column_type)
        if serial and re.fullmatch(
                r"nextval\('(?:[\w\"]+\.)?\"?" + re.escape(f"{table}_{column['name']}_seq") + r"\"?'::regclass\)", default):
            column_type, default = serial, ""  # a serial column: its sequence is created with it
        line = f"    {q(column['name'])} {column_type}"
        if column["identity"]:
            line += " GENERATED " + ("ALWAYS" if column["identity"] == "a" else "BY DEFAULT") + " AS IDENTITY"
        elif column["generated"]:
            line += f" GENERATED ALWAYS AS ({default}) " + ("STORED" if column["generated"] == "s" else "VIRTUAL")
        elif default:
            line += f" DEFAULT {default}"
        if not column["nullable"]:
            line += " NOT NULL"
        lines.append(line)
    constraints = table_constraints(conn, schema, table)
    lines += [f"    CONSTRAINT {q(c['name'])} {c['definition']}" for c in constraints]
    statements = [f"CREATE TABLE {q(schema)}.{q(table)} (\n" + ",\n".join(lines) + "\n);"]
    # Indexes that back a constraint are already part of the CREATE TABLE above.
    owned = {c["name"] for c in constraints}
    statements += [index["definition"] + ";" for index in table_indexes(conn, schema, table)
                   if not index["primary"] and index["name"] not in owned]
    return "\n\n".join(statements)


def list_schemas(conn):
    rows = conn.execute(
        "SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg\\_%' AND nspname <> 'information_schema' ORDER BY 1"
    ).fetchall()
    return [row[0] for row in rows]


KINDS = {"r": "table", "p": "partitioned table", "v": "view", "m": "materialized view", "f": "foreign table", "S": "sequence"}


def list_tables(conn, schema, system=False):
    """Tables, views, materialized views, foreign tables and sequences of a schema (callers pick the kinds they need)."""
    hide = "" if system else " AND " + NOT_IN_EXTENSION.format(oid="c.oid", catalog="pg_class")
    rows = conn.execute(
        f"""SELECT c.relname, c.relkind::text, CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S') AND NOT c.relispartition{hide}
            ORDER BY c.relname""", (schema,)).fetchall()
    return [{"name": name, "kind": KINDS[kind], "estimate": estimate} for name, kind, estimate in rows]


def view_ddl(conn, schema, name):
    """CREATE VIEW / CREATE MATERIALIZED VIEW (with its indexes) for a view or materialized view."""
    info = relation_info(conn, schema, name, kinds=("v", "m"))
    body = conn.execute("SELECT pg_get_viewdef(c.oid, true) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = %s AND c.relname = %s", (schema, name)).fetchone()[0].strip().rstrip(";")
    qualified = f"{_quote(conn, schema)}.{_quote(conn, name)}"
    if info["relkind"] == "v":
        return f"CREATE OR REPLACE VIEW {qualified} AS\n{body};"
    statements = [f"CREATE MATERIALIZED VIEW {qualified} AS\n{body}\nWITH {'DATA' if info['populated'] else 'NO DATA'};"]
    statements += [index["definition"] + ";" for index in table_indexes(conn, schema, name)]
    return "\n\n".join(statements)


TRIGGER_EVENTS = ((4, "INSERT"), (8, "DELETE"), (16, "UPDATE"), (32, "TRUNCATE"))
TRIGGER_STATES = {"O": "enabled", "D": "disabled", "R": "enabled (replica)", "A": "enabled (always)"}


def table_triggers(conn, schema, table):
    rows = conn.execute(
        """SELECT t.tgname, pg_get_triggerdef(t.oid), t.tgenabled::text, t.tgtype::int, p.proname
           FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
           JOIN pg_proc p ON p.oid = t.tgfoid
           WHERE n.nspname = %s AND c.relname = %s AND NOT t.tgisinternal
           ORDER BY t.tgname""", (schema, table)).fetchall()
    result = []
    for name, definition, enabled, tgtype, function in rows:
        timing = "INSTEAD OF" if tgtype & 64 else ("BEFORE" if tgtype & 2 else "AFTER")
        result.append({"name": name, "definition": definition, "state": TRIGGER_STATES.get(enabled, enabled), "function": function,
                       "timing": timing, "events": " OR ".join(label for bit, label in TRIGGER_EVENTS if tgtype & bit),
                       "level": "ROW" if tgtype & 1 else "STATEMENT"})
    return result


def list_functions(conn, schema, procedures=False, system=False):
    """Functions (including aggregates and window functions) or, with procedures=True, procedures of a schema."""
    hide = "" if system else " AND " + NOT_IN_EXTENSION.format(oid="p.oid", catalog="pg_proc")
    kinds = "('p')" if procedures else "('f', 'a', 'w')"
    rows = conn.execute(
        f"""SELECT p.oid::bigint, p.proname, pg_get_function_identity_arguments(p.oid),
                   CASE WHEN p.prokind = 'p' THEN NULL ELSE pg_get_function_result(p.oid) END, l.lanname, p.prokind::text
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_language l ON l.oid = p.prolang
            WHERE n.nspname = %s AND p.prokind IN {kinds}{hide}
            ORDER BY p.proname, 3""", (schema,)).fetchall()
    return [{"oid": oid, "name": name, "args": args, "signature": f"{name}({args})", "returns": returns or "",
             "language": language, "kind": {"f": "function", "a": "aggregate", "w": "window function", "p": "procedure"}[kind]}
            for oid, name, args, returns, language, kind in rows]


VOLATILITY = {"i": "immutable", "s": "stable", "v": "volatile"}
PARALLEL = {"s": "safe", "r": "restricted", "u": "unsafe"}


def function_info(conn, schema, oid):
    """Properties and source of one function or procedure, looked up by oid but only inside the named schema."""
    try:
        oid = int(oid)
    except (TypeError, ValueError):
        raise PgError("That function was not found in this schema.")
    row = conn.execute(
        """SELECT p.proname, pg_get_userbyid(p.proowner), l.lanname, p.prokind::text,
                  pg_get_function_arguments(p.oid), CASE WHEN p.prokind = 'p' THEN NULL ELSE pg_get_function_result(p.oid) END,
                  p.provolatile::text, p.prosecdef, p.proisstrict, p.proparallel::text, obj_description(p.oid, 'pg_proc'),
                  CASE WHEN p.prokind = 'a' THEN NULL ELSE pg_get_functiondef(p.oid) END
           FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_language l ON l.oid = p.prolang
           WHERE n.nspname = %s AND p.oid = %s""", (schema, oid)).fetchone()
    if row is None:
        raise PgError("That function was not found in this schema.")
    name, owner, language, kind, args, returns, volatility, secdef, strict, parallel, comment, ddl = row
    return {"name": name, "owner": owner, "language": language, "arguments": args, "returns": returns or "",
            "kind": {"f": "function", "a": "aggregate", "w": "window function", "p": "procedure"}[kind],
            "volatility": VOLATILITY.get(volatility, volatility), "security": "definer" if secdef else "invoker",
            "strict": strict, "parallel": PARALLEL.get(parallel, parallel), "comment": comment or "",
            "ddl": ddl or f"-- {name} is an aggregate; PostgreSQL has no single definition to display."}


def sequence_info(conn, schema, name):
    relation_info(conn, schema, name, kinds=("S",))  # raises PgError when missing
    row = conn.execute(
        """SELECT sequenceowner, data_type::text, start_value, min_value, max_value, increment_by, cycle, cache_size, last_value
           FROM pg_sequences WHERE schemaname = %s AND sequencename = %s""", (schema, name)).fetchone()
    owner, data_type, start, minimum, maximum, increment, cycle, cache, last = row
    owned = conn.execute(
        """SELECT t.relname, a.attname
           FROM pg_class s JOIN pg_namespace n ON n.oid = s.relnamespace
           JOIN pg_depend d ON d.objid = s.oid AND d.classid = 'pg_class'::regclass AND d.refclassid = 'pg_class'::regclass
                AND d.deptype IN ('a', 'i')
           JOIN pg_class t ON t.oid = d.refobjid JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid
           WHERE n.nspname = %s AND s.relname = %s""", (schema, name)).fetchone()
    return {"owner": owner, "type": data_type, "start": start, "min": minimum, "max": maximum, "increment": increment,
            "cycle": cycle, "cache": cache, "last": last, "owned_by": f"{owned[0]}.{owned[1]}" if owned else ""}


def sequence_ddl(conn, schema, name):
    info = sequence_info(conn, schema, name)
    qualified = f"{_quote(conn, schema)}.{_quote(conn, name)}"
    ddl = (f"CREATE SEQUENCE {qualified}\n    AS {info['type']}\n    INCREMENT BY {info['increment']}\n    MINVALUE {info['min']}"
           f"\n    MAXVALUE {info['max']}\n    START WITH {info['start']}\n    CACHE {info['cache']}\n    {'' if info['cycle'] else 'NO '}CYCLE")
    if info["owned_by"]:
        table, column = info["owned_by"].split(".", 1)
        ddl += f"\n    OWNED BY {_quote(conn, schema)}.{_quote(conn, table)}.{_quote(conn, column)}"
    return ddl + ";"


TYPE_KINDS = {"e": "enum", "d": "domain", "c": "composite", "r": "range"}


def list_types(conn, schema, system=False):
    """Enums, domains, standalone composite types and range types of a schema."""
    hide = "" if system else " AND " + NOT_IN_EXTENSION.format(oid="t.oid", catalog="pg_type")
    rows = conn.execute(
        f"""SELECT t.typname, t.typtype::text FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
            LEFT JOIN pg_class rel ON rel.oid = t.typrelid
            WHERE n.nspname = %s AND t.typtype IN ('e', 'd', 'c', 'r') AND (t.typtype <> 'c' OR rel.relkind = 'c'){hide}
            ORDER BY t.typname""", (schema,)).fetchall()
    return [{"name": name, "kind": TYPE_KINDS[kind]} for name, kind in rows]


def type_info(conn, schema, name):
    row = conn.execute(
        """SELECT t.oid::bigint, t.typtype::text, pg_get_userbyid(t.typowner), t.typrelid::bigint,
                  CASE WHEN t.typtype = 'd' THEN format_type(t.typbasetype, t.typtypmod) END, t.typdefault, t.typnotnull,
                  obj_description(t.oid, 'pg_type')
           FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace LEFT JOIN pg_class rel ON rel.oid = t.typrelid
           WHERE n.nspname = %s AND t.typname = %s AND t.typtype IN ('e', 'd', 'c', 'r')
             AND (t.typtype <> 'c' OR rel.relkind = 'c')""", (schema, name)).fetchone()
    if row is None:
        raise PgError(f"Type {schema}.{name} was not found.")
    oid, kind, owner, relid, base, default, notnull, comment = row
    info = {"name": name, "kind": TYPE_KINDS[kind], "owner": owner, "comment": comment or "", "labels": [], "fields": [],
            "base": base or "", "default": default or "", "not_null": notnull, "constraints": [], "subtype": ""}
    if kind == "e":
        info["labels"] = [r[0] for r in conn.execute("SELECT enumlabel FROM pg_enum WHERE enumtypid = %s ORDER BY enumsortorder", (oid,))]
    elif kind == "c":
        info["fields"] = [{"name": n, "type": t} for n, t in conn.execute(
            """SELECT attname, format_type(atttypid, atttypmod) FROM pg_attribute
               WHERE attrelid = %s AND attnum > 0 AND NOT attisdropped ORDER BY attnum""", (relid,))]
    elif kind == "d":
        info["constraints"] = [{"name": n, "definition": d} for n, d in conn.execute(
            # PostgreSQL 18 records NOT NULL as a constraint too; it is already shown as "Not null" / NOT NULL.
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE contypid = %s AND contype <> 'n' ORDER BY conname", (oid,))]
    elif kind == "r":
        info["subtype"] = conn.execute("SELECT format_type(rngsubtype, NULL) FROM pg_range WHERE rngtypid = %s", (oid,)).fetchone()[0]
    return info


def type_ddl(conn, schema, name):
    info = type_info(conn, schema, name)
    qualified = f"{_quote(conn, schema)}.{_quote(conn, name)}"
    if info["kind"] == "enum":
        labels = ", ".join("'" + label.replace("'", "''") + "'" for label in info["labels"])
        return f"CREATE TYPE {qualified} AS ENUM ({labels});"
    if info["kind"] == "composite":
        fields = ",\n".join(f"    {_quote(conn, f['name'])} {f['type']}" for f in info["fields"])
        return f"CREATE TYPE {qualified} AS (\n{fields}\n);"
    if info["kind"] == "range":
        return f"CREATE TYPE {qualified} AS RANGE (\n    SUBTYPE = {info['subtype']}\n);"
    ddl = f"CREATE DOMAIN {qualified} AS {info['base']}"
    if info["default"]:
        ddl += f"\n    DEFAULT {info['default']}"
    if info["not_null"]:
        ddl += "\n    NOT NULL"
    ddl += "".join(f"\n    CONSTRAINT {_quote(conn, c['name'])} {c['definition']}" for c in info["constraints"])
    return ddl + ";"


def table_columns(conn, schema, table):
    # pg_attribute covers views and materialized views too, unlike information_schema.columns.
    rows = conn.execute(
        """SELECT a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull,
                  pg_get_expr(d.adbin, d.adrelid), a.attidentity::text, a.attgenerated::text
           FROM pg_attribute a
           JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
           LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
           WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped
           ORDER BY a.attnum""", (schema, table)).fetchall()
    # For a generated column `default` holds its expression; `identity` is "a" (ALWAYS) or "d" (BY DEFAULT).
    return [{"name": n, "type": t, "nullable": nullable, "default": default or "", "identity": identity, "generated": generated}
            for n, t, nullable, default, identity, generated in rows]


def display(value):
    """Text shown in a result cell."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
        return "\\x" + data[:32].hex() + ("…" if len(data) > 32 else "")
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return str(value)


def fetch_rows(conn, schema, table, page):
    columns = table_columns(conn, schema, table)
    if not columns:
        raise PgError(f"{schema}.{table} does not exist or has no columns.")
    page = max(int(page or 1), 1)
    query = sql.SQL("SELECT * FROM {}.{} LIMIT %s OFFSET %s").format(sql.Identifier(schema), sql.Identifier(table))
    rows = conn.execute(query, (PAGE_SIZE + 1, (page - 1) * PAGE_SIZE)).fetchall()
    return {
        "columns": columns, "page": page,
        "rows": [[display(value) for value in row] for row in rows[:PAGE_SIZE]],
        "has_next": len(rows) > PAGE_SIZE,
    }


def run_sql(server, statement, database=None):
    """Run user SQL in a read-only transaction with a timeout and a row cap."""
    started = time.monotonic()
    with connect(server, database) as conn:
        cursor = conn.execute(statement)
        elapsed = int((time.monotonic() - started) * 1000)
        if cursor.description is None:
            return {"columns": [], "rows": [], "truncated": False, "count": 0,
                    "message": cursor.statusmessage or "OK", "ms": elapsed}
        limit = int(settings.MAX_ROWS)
        rows = cursor.fetchmany(limit + 1)
        return {
            "columns": [column.name for column in cursor.description],
            "rows": [[display(value) for value in row] for row in rows[:limit]],
            "truncated": len(rows) > limit, "count": min(len(rows), limit), "message": "", "ms": elapsed,
        }
