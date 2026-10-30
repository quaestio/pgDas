"""PostgreSQL access for PgDeck. Everything here runs in a READ ONLY transaction enforced by the server."""
import json
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


def schema_rows(conn):
    rows = conn.execute(
        """SELECT nspname, pg_get_userbyid(nspowner) FROM pg_namespace
           WHERE nspname NOT LIKE 'pg\\_%' AND nspname <> 'information_schema' ORDER BY 1""").fetchall()
    return [{"name": name, "owner": owner} for name, owner in rows]


def table_info(conn, schema, table):
    row = conn.execute(
        """SELECT pg_get_userbyid(c.relowner), c.relkind, GREATEST(c.reltuples, 0)::bigint,
                  pg_total_relation_size(c.oid), obj_description(c.oid, 'pg_class')
           FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r', 'p')""", (schema, table)).fetchone()
    if row is None:
        raise PgError(f"Table {schema}.{table} was not found.")
    owner, kind, estimate, size, comment = row
    return {"owner": owner, "kind": KINDS[kind], "estimate": estimate, "size_text": size_text(size), "comment": comment or ""}


CONSTRAINT_TYPES = {"p": "PRIMARY KEY", "f": "FOREIGN KEY", "u": "UNIQUE", "c": "CHECK", "x": "EXCLUDE", "t": "TRIGGER"}


def table_constraints(conn, schema, table):
    rows = conn.execute(
        """SELECT k.conname, k.contype, pg_get_constraintdef(k.oid)
           FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = %s AND c.relname = %s
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


def _quote(conn, name):
    return conn.execute("SELECT quote_ident(%s)", (name,)).fetchone()[0]


def table_ddl(conn, schema, table):
    """A readable CREATE TABLE for a table, rebuilt from the catalogs (PostgreSQL has no built-in for this)."""
    table_info(conn, schema, table)  # raises PgError when missing
    q = lambda name: _quote(conn, name)
    lines = []
    for column in table_columns(conn, schema, table):
        line = f"    {q(column['name'])} {column['type']}"
        if column["default"]:
            line += f" DEFAULT {column['default']}"
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


KINDS = {"r": "table", "p": "partitioned table", "v": "view", "m": "materialized view", "f": "foreign table"}


def list_tables(conn, schema):
    rows = conn.execute(
        """SELECT c.relname, c.relkind, GREATEST(c.reltuples, 0)::bigint
           FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'f') AND NOT c.relispartition
           ORDER BY c.relname""", (schema,)).fetchall()
    return [{"name": name, "kind": KINDS[kind], "estimate": estimate} for name, kind, estimate in rows]


def table_columns(conn, schema, table):
    # pg_attribute covers views and materialized views too, unlike information_schema.columns.
    rows = conn.execute(
        """SELECT a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull,
                  pg_get_expr(d.adbin, d.adrelid)
           FROM pg_attribute a
           JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
           LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
           WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped
           ORDER BY a.attnum""", (schema, table)).fetchall()
    return [{"name": n, "type": t, "nullable": nullable, "default": default or ""} for n, t, nullable, default in rows]


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
