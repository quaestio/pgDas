"""Writing rows out as CSV, JSON or SQL INSERT statements, one chunk of text at a time so a big table never sits in memory.

  csv   a header row, then the rows; a value that starts with = + - @ is prefixed with ' so a spreadsheet cannot run it as a formula
  json  one array of objects
  sql   INSERT INTO "schema"."table" (columns) VALUES (...), (...); in batches (for loading the rows into another database)

The caller gives the column names and an iterable of rows. For csv and sql the values should be PostgreSQL's own text of each
cell (SELECT col::text), so every type round-trips; json takes native Python values.
"""
import csv
import io
import json
import re

FORMATS = {"csv": ("text/csv; charset=utf-8", "csv"), "json": ("application/json; charset=utf-8", "json"),
           "sql": ("application/sql; charset=utf-8", "sql")}
BATCH = 100
_NUMBER = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")
_NUMERIC_TYPES = ("smallint", "integer", "bigint", "numeric", "decimal", "real", "double precision")


def formula_safe(value):
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text


def _ident(name):
    return '"' + name.replace('"', '""') + '"'


def sql_literal(value, type_name=""):
    """A value as SQL text. Numbers and booleans stay bare; everything else is a quoted string PostgreSQL converts to the column type."""
    if value is None:
        return "NULL"
    text = str(value)
    if type_name.startswith(_NUMERIC_TYPES) and _NUMBER.match(text):
        return text
    if type_name == "boolean" and text in ("true", "false"):
        return text
    return "'" + text.replace("'", "''") + "'"


def stream(fmt, columns, rows, table=None, types=None):
    """Yield the file as text chunks. `table` is (schema, name), needed for sql; `types` the column type names, for sql."""
    if fmt == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([formula_safe(c) for c in columns])
        yield buffer.getvalue()
        for row in rows:
            buffer.seek(0); buffer.truncate()
            writer.writerow([formula_safe(value) for value in row])
            yield buffer.getvalue()
    elif fmt == "json":
        yield "["
        first = True
        for row in rows:
            yield ("\n" if first else ",\n") + json.dumps(dict(zip(columns, row)), default=str, ensure_ascii=False)
            first = False
        yield "\n]\n" if not first else "]\n"
    elif fmt == "sql":
        if table is None:
            raise ValueError("sql export needs the table name")
        types = types or [""] * len(columns)
        head = f"INSERT INTO {_ident(table[0])}.{_ident(table[1])} ({', '.join(_ident(c) for c in columns)}) VALUES\n"
        yield f"-- {table[0]}.{table[1]}, exported by PGDAS\n"
        batch = []
        for row in rows:
            batch.append("  (" + ", ".join(sql_literal(value, kind) for value, kind in zip(row, types)) + ")")
            if len(batch) == BATCH:
                yield head + ",\n".join(batch) + ";\n"
                batch = []
        if batch:
            yield head + ",\n".join(batch) + ";\n"
    else:
        raise ValueError("unknown format")
