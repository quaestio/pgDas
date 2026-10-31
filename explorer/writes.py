"""Deleting rows, and putting them back.

PgDeck is read-only everywhere except here. A DELETE goes through three steps, and the audit log keeps every deleted row:

  1. preview_delete  runs the statement inside a transaction that is ALWAYS rolled back, and reports how many rows it
                     would delete, a sample of them, and any other tables it would also change (ON DELETE CASCADE, triggers).
  2. (the person reads the popup and types ZAP)
  3. execute_delete  runs it again, checks the result is exactly what was confirmed, saves the deleted rows in the audit
                     log as JSON, and only then commits. If the audit entry cannot be saved, nothing is deleted.

reinsert() puts the rows of a recorded delete back, once.
"""
import re

import sqlparse
from django.conf import settings
from django.utils import timezone
from psycopg import sql
from psycopg.types.json import Jsonb
from sqlparse import tokens as T

from audit.models import record

from . import pg
from .pg import PgError

CONFIRM_WORD = "ZAP"      # typed exactly like this, capital letters, to confirm a delete
SAMPLE_ROWS = 5

_IDENT = r'(?:"(?:[^"]|"")+"|[A-Za-z_][\w$]*)'
_DELETE = re.compile(rf"^DELETE\s+FROM\s+(?:ONLY\s+)?(?P<table>{_IDENT}(?:\s*\.\s*{_IDENT})?)(?P<rest>.*)$", re.I | re.S)
_ALIAS = re.compile(rf"^\s+(?:AS\s+)?(?P<alias>{_IDENT})(?=\s|$)", re.I)
_NOT_ALIASES = {"USING", "WHERE", "RETURNING"}


def prepare_statement(text, verb, allow_returning=False):
    """The checks every changing statement goes through. Returns (statement text without comments, sqlparse tree).

    It must be exactly one `verb` statement (DELETE, UPDATE, ...) with balanced parentheses, and no WITH in front. PgDeck wraps
    some statements to capture what they change, so each must be one self-contained statement.
    """
    statements = [s for s in sqlparse.split(text or "") if s.strip(" \t\r\n;")]
    if not statements:
        raise PgError(f"Enter a {verb} statement.")
    if len(statements) > 1:
        raise PgError(f"Run one {verb} statement at a time.")
    cleaned = sqlparse.format(statements[0], strip_comments=True).strip().rstrip(";").strip()
    first_word = cleaned.split(None, 1)[0].upper() if cleaned else ""
    if first_word == "WITH":
        raise PgError(f"{verb} with a WITH clause is not supported yet. Write it as a plain {verb} statement.")
    if first_word != verb:
        raise PgError(f"This is not a {verb} statement.")
    parsed = sqlparse.parse(cleaned)[0]
    depth = 0
    for token in parsed.flatten():
        if token.ttype in T.Keyword and token.value.upper() == "RETURNING" and not allow_returning:
            raise PgError("Remove the RETURNING clause: PgDeck records what changed itself.")
        if token.ttype in T.Punctuation:
            if token.value == "(":
                depth += 1
            elif token.value == ")":
                depth -= 1
                if depth < 0:
                    raise PgError("The parentheses in this statement do not match.")
            elif token.value == ";":
                raise PgError(f"Run one {verb} statement at a time.")
    if depth != 0:
        raise PgError("The parentheses in this statement do not match.")
    return cleaned, parsed


def parse_delete(text):
    """Check that `text` is exactly one plain DELETE and pull out the table it deletes from.

    Returns {"statement", "table_ref", "target", "has_where"}. Anything else is refused with a plain message.
    """
    cleaned, parsed = prepare_statement(text, "DELETE")
    match = _DELETE.match(cleaned)
    if not match:
        raise PgError("Write it as DELETE FROM table [AS alias] [USING …] [WHERE …].")
    table = re.sub(r"\s*\.\s*", ".", match["table"])
    alias_match = _ALIAS.match(match["rest"])
    alias = alias_match["alias"] if alias_match and alias_match["alias"].upper() not in _NOT_ALIASES else None
    has_where = any(isinstance(token, sqlparse.sql.Where) for token in parsed.tokens)
    return {"statement": cleaned, "table_ref": table, "target": alias or table, "has_where": has_where}


def _resolve_table(conn, table_ref):
    row = conn.execute(
        """SELECT n.nspname, c.relname, c.relkind::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE c.oid = to_regclass(%s)""", (table_ref,)).fetchone()
    if row is None:
        raise PgError(f"Table {table_ref} was not found.")
    schema, table, relkind = row
    if relkind not in ("r", "p"):
        raise PgError("Only ordinary tables are supported here (not views, sequences or foreign tables).")
    return schema, table


def _delete_and_capture(conn, parsed):
    """Run the DELETE and return every deleted row as a JSON object. The caller decides whether to commit."""
    wrapped = (f"WITH pgdeck_deleted AS ({parsed['statement']} RETURNING {parsed['target']}.*) "
               "SELECT to_jsonb(pgdeck_deleted) FROM pgdeck_deleted")
    limit = int(settings.MAX_DELETE_ROWS)
    rows = [row[0] for row in conn.execute(wrapped).fetchmany(limit + 1)]
    if len(rows) > limit:
        raise PgError(f"This would delete more than {limit:,} rows, more than PgDeck can record for re-insertion. "
                      "Narrow the WHERE clause, or delete in smaller batches.")
    return rows


def _in_table_order(conn, schema, table, rows):
    """to_jsonb sorts keys by length, then alphabet. Put each row's keys back in the table's own column order."""
    order = [c["name"] for c in pg.table_columns(conn, schema, table)]
    ordered = [{**{name: row[name] for name in order if name in row}, **{k: v for k, v in row.items() if k not in order}}
               for row in rows]
    return order, ordered


def _side_effects(conn, schema, table, own="deleted"):
    """Other tables changed by this transaction so far (cascades, triggers): [{"table", "inserted", "updated", "deleted"}].

    `own` is what the statement itself does to its own table ("deleted", "updated" or "inserted"): that is the count the
    person is shown, so only anything else happening to that table is reported here.
    """
    changed = conn.execute(
        """SELECT schemaname, relname, n_tup_ins, n_tup_upd, n_tup_del FROM pg_stat_xact_user_tables
           WHERE n_tup_ins + n_tup_upd + n_tup_del > 0 ORDER BY 1, 2""").fetchall()
    effects = []
    for s, t, inserted, updated, deleted in changed:
        counts = {"inserted": inserted, "updated": updated, "deleted": deleted}
        if (s, t) == (schema, table):
            counts[own] = 0
            if not any(counts.values()):
                continue                               # only its own change, which is already counted
        effects.append({"table": f"{s}.{t}", **counts})
    return effects


def preview_delete(server, database, text):
    """What a DELETE would do, without doing it: the transaction is always rolled back."""
    parsed = parse_delete(text)
    with pg.connect_rw(server, database or None) as conn:
        schema, table = _resolve_table(conn, parsed["table_ref"])
        rows = _delete_and_capture(conn, parsed)
        effects = _side_effects(conn, schema, table)
        columns, rows = _in_table_order(conn, schema, table, rows) if rows else ([], rows)
        conn.rollback()
    return {"statement": parsed["statement"], "schema": schema, "table": table, "count": len(rows),
            "has_where": parsed["has_where"], "side_effects": effects,
            "columns": columns, "sample": rows[:SAMPLE_ROWS]}


def execute_delete(server, database, text, expected_count, expected_side_effects, confirm, user):
    """Delete for real, but only if the person confirmed with ZAP and nothing has changed since the preview."""
    if confirm != CONFIRM_WORD:                         # exact, case-sensitive: "zap", "Zap" and " ZAP" are refused
        raise PgError(f"Type {CONFIRM_WORD} in capital letters to confirm.")
    parsed = parse_delete(text)
    with pg.connect_rw(server, database or None) as conn:
        schema, table = _resolve_table(conn, parsed["table_ref"])
        rows = _delete_and_capture(conn, parsed)
        effects = _side_effects(conn, schema, table)
        if not rows:
            raise PgError("No rows match, so there is nothing to delete.")
        if len(rows) != expected_count or effects != expected_side_effects:
            raise PgError(f"The data changed since you checked: {expected_count} rows were expected and "
                          f"{len(rows)} match now. Nothing was deleted. Run the statement again to check.")
        actual_database = conn.execute("SELECT current_database()").fetchone()[0]
        columns, rows = _in_table_order(conn, schema, table, rows)
        payload = {"kind": "delete", "database": actual_database, "schema": schema, "table": table,
                   "statement": parsed["statement"], "count": len(rows), "columns": columns,
                   "no_where": not parsed["has_where"], "side_effects": effects, "rows": rows}
        # Save the record BEFORE committing: if this fails, the transaction rolls back and nothing is deleted.
        try:
            entry = record(user, "delete", server, parsed["statement"], ok=True, rows=len(rows), payload=payload)
        except Exception:
            raise PgError("PgDeck could not save the audit record, so nothing was deleted.")
        try:
            conn.commit()
        except Exception as error:                      # the database refused the commit: say so in the record
            entry.ok = False
            entry.detail = f"{entry.detail}\n(not committed: {str(error)[:200]})"
            entry.save(update_fields=["ok", "detail"])
            raise PgError("The database did not accept the delete, so nothing was deleted.")
    return {"count": len(rows), "schema": schema, "table": table, "entry_id": entry.pk, "side_effects": effects}


def _insertable_columns(conn, schema, table):
    """Columns that can be given a value in INSERT: stored/virtual generated columns cannot."""
    return [c["name"] for c in pg.table_columns(conn, schema, table) if not c["generated"]]


def reinsert(entry, user):
    """Put the rows of a recorded delete back into their table. All or nothing, and only once per record."""
    if not entry.is_delete_record:
        raise PgError("This audit entry is not a recorded delete.")
    if entry.restored_at is not None:
        raise PgError("These rows were already re-inserted.")
    if not entry.ok or not entry.payload.get("rows"):
        raise PgError("There are no rows to re-insert in this entry.")
    server = entry.server
    if server is None:
        raise PgError("The server this was deleted from is no longer saved in PgDeck.")
    payload = entry.payload
    schema, table, rows = payload["schema"], payload["table"], payload["rows"]
    with pg.connect_rw(server, payload["database"]) as conn:
        _resolve_table(conn, f"{pg._quote(conn, schema)}.{pg._quote(conn, table)}")
        columns = _insertable_columns(conn, schema, table)
        missing = [name for name in payload["columns"] if name not in columns and name not in
                   {c["name"] for c in pg.table_columns(conn, schema, table)}]
        if missing:
            raise PgError(f"The table no longer has the column{'s' if len(missing) > 1 else ''} {', '.join(missing)}, "
                          "so the rows cannot be put back as they were.")
        use = [name for name in columns if name in payload["columns"]]
        cols = sql.SQL(", ").join(sql.Identifier(name) for name in use)
        query = sql.SQL("INSERT INTO {}.{} ({}) OVERRIDING SYSTEM VALUE SELECT {} FROM jsonb_populate_recordset(NULL::{}.{}, %s)").format(
            sql.Identifier(schema), sql.Identifier(table), cols, cols, sql.Identifier(schema), sql.Identifier(table))
        inserted = conn.execute(query, (Jsonb(rows),)).rowcount
        if inserted != len(rows):
            raise PgError(f"Only {inserted} of {len(rows)} rows could be inserted, so nothing was changed.")
        now = timezone.now()
        entry.restored_at, entry.restored_by = now, user.get_username()
        entry.save(update_fields=["restored_at", "restored_by"])
        restore_entry = record(user, "delete.restore", server, f"Re-inserted {inserted} rows into {schema}.{table} "
                               f"(from audit entry #{entry.pk})", rows=inserted,
                               payload={"kind": "restore", "restored_entry": entry.pk, "database": payload["database"],
                                        "schema": schema, "table": table, "count": inserted})
        try:
            conn.commit()
        except Exception:
            entry.restored_at, entry.restored_by = None, ""     # undo the bookkeeping: the rows were not put back
            entry.save(update_fields=["restored_at", "restored_by"])
            restore_entry.delete()
            raise PgError("The database did not accept the re-insert, so nothing was changed.")
    return {"count": inserted, "schema": schema, "table": table}
