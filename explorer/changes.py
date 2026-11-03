"""INSERT, UPDATE, TRUNCATE, DROP, CREATE and ALTER, and the one entry point (preview / execute) the SQL runner uses for
every changing statement. DELETE, with its row-by-row JSON record and re-insert, lives in writes.py.

  statement   what happens                                      confirmation
  INSERT      runs at once, recorded in the audit log           none
  UPDATE      popup: number of rows, warning if no WHERE        a button
  DELETE      popup: number of rows, no rollback                type ZAP
  TRUNCATE    popup: rows lost per table, no rollback           type TRUNCATE
  DROP        popup: what goes with it, no rollback             type DROP
  CREATE      popup: exactly what would be created              a button
  ALTER       popup: exactly what would change                  a button, or type ALTER when it is risky

Every one of them: needs a staff user and the server's own switch; one plain statement at a time; checked first in a
transaction that is always rolled back; run for real only if the checked facts are still true; recorded in the audit log
(as JSON) BEFORE the commit, so nothing changes without a record.
"""
import json
import re

import psycopg
import sqlparse
from django.conf import settings
from django.utils import timezone
from psycopg import sql
from psycopg.types.json import Jsonb

from audit.models import record

from . import pg, writes
from .pg import PgError
from .writes import SAMPLE_ROWS, _IDENT, _in_table_order, _resolve_table, _side_effects, prepare_statement

# The word to type. ALTER asks for its word only when the change is risky (see _alter_is_risky); CREATE never does.
CONFIRM_WORDS = {"delete": writes.CONFIRM_WORD, "update": None, "insert": None, "truncate": "TRUNCATE", "drop": "DROP",
                 "create": None, "alter": "ALTER", "grant": None, "revoke": None}
STRUCTURE_KINDS = ("truncate", "drop", "create", "alter")   # these need the server's second switch, "Also allow CREATE, ALTER, TRUNCATE and DROP"
_TABLE_REF = rf"{_IDENT}(?:\s*\.\s*{_IDENT})?"


def classify(text):
    """Which changing statement this is: insert, update, delete, truncate, drop, create or alter. Anything else is not one."""
    bare = sqlparse.format(text or "", strip_comments=True).strip()
    kind = (bare.split(None, 1)[0].upper().rstrip(";").lower() if bare else "")
    if kind not in CONFIRM_WORDS:
        raise PgError("Only INSERT, UPDATE, DELETE, TRUNCATE, DROP, CREATE, ALTER, GRANT and REVOKE can change things here; everything else runs read-only.")
    return kind


def _normalise(ref):
    return re.sub(r"\s*\.\s*", ".", ref)


def _run(conn, statement, params=None):
    """Run a statement; a database error becomes a plain message with its DETAIL and HINT (they say what depends on what)."""
    try:
        return conn.execute(statement, params)
    except psycopg.Error as error:
        diag = error.diag
        detail = "; ".join(line.strip() for line in (diag.message_detail or "").splitlines() if line.strip())   # one line per object
        parts = [diag.message_primary or str(error), detail, diag.message_hint]
        raise PgError(" ".join(part.strip() for part in parts if part))


def _count(conn, schema, table):
    return conn.execute(sql.SQL("SELECT count(*) FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(table))).fetchone()[0]


def _commit_after_recording(conn, server, user, kind, detail, rows, payload, what):
    """Save the audit record first, then commit. If the record cannot be saved nothing changes; if the commit is refused
    the record is marked as not committed."""
    try:
        entry = record(user, kind, server, detail, ok=True, rows=rows, payload=payload)
    except Exception:
        raise PgError(f"PGDAS could not save the audit record, so nothing was {what}.")
    try:
        conn.commit()
    except Exception as error:
        entry.ok = False
        entry.detail = f"{entry.detail}\n(not committed: {str(error)[:200]})"
        entry.save(update_fields=["ok", "detail"])
        raise PgError(f"The database did not accept the change, so nothing was {what}.")
    return entry


def _current_database(conn):
    return conn.execute("SELECT current_database()").fetchone()[0]


def _changed_since(what_was_expected):
    return PgError(f"The data changed since you checked: {what_was_expected} Nothing was changed. Run the statement again to check.")


# ------------------------------------------------------------------------------------------------------------- UPDATE
_UPDATE = re.compile(rf"^UPDATE\s+(?:ONLY\s+)?(?P<table>{_TABLE_REF})(?P<rest>.*)$", re.I | re.S)
_UPDATE_NOT_ALIASES = {"SET", "WHERE", "FROM", "RETURNING"}


def parse_update(text):
    cleaned, parsed = prepare_statement(text, "UPDATE")
    match = _UPDATE.match(cleaned)
    if not match:
        raise PgError("Write it as UPDATE table [AS alias] SET … [WHERE …].")
    table = _normalise(match["table"])
    alias_match = writes._ALIAS.match(match["rest"])
    alias = alias_match["alias"] if alias_match and alias_match["alias"].upper() not in _UPDATE_NOT_ALIASES else None
    has_where = any(isinstance(token, sqlparse.sql.Where) for token in parsed.tokens)
    return {"statement": cleaned, "table_ref": table, "target": alias or table, "has_where": has_where}


def _update_and_count(conn, parsed):
    """Run the UPDATE; return how many rows it changed and the first few rows as they look afterwards."""
    wrapped = (f"WITH pgdas_updated AS ({parsed['statement']} RETURNING {parsed['target']}.*) "
               "SELECT (SELECT count(*) FROM pgdas_updated), "
               "(SELECT coalesce(jsonb_agg(to_jsonb(s)), '[]'::jsonb) "
               f"FROM (SELECT * FROM pgdas_updated LIMIT {SAMPLE_ROWS}) s)")
    count, sample = _run(conn, wrapped).fetchone()
    return count, sample


def _qualified(conn, schema, table):
    return f"{pg._quote(conn, schema)}.{pg._quote(conn, table)}"


def _primary_key(conn, schema, table):
    rows = conn.execute(
        """SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
           WHERE i.indrelid = to_regclass(%s) AND i.indisprimary ORDER BY array_position(i.indkey::int2[], a.attnum)""",
        (_qualified(conn, schema, table),)).fetchall()
    return [row[0] for row in rows]


def _key_of(row, pk):
    return tuple(json.dumps(row.get(column), sort_keys=True) for column in pk)


def _key_match(pk, left, right):
    return sql.SQL(" AND ").join(sql.SQL("{}.{} = {}.{}").format(sql.Identifier(left), sql.Identifier(c), sql.Identifier(right),
                                                                 sql.Identifier(c)) for c in pk)


def _update_rows(conn, parsed, limit):
    """Run the UPDATE and return the rows as they look afterwards (JSON), at most limit + 1 of them."""
    wrapped = f"WITH pgdas_updated AS ({parsed['statement']} RETURNING {parsed['target']}.*) SELECT to_jsonb(pgdas_updated) FROM pgdas_updated"
    return [row[0] for row in _run(conn, wrapped).fetchmany(limit + 1)]


def _old_rows(conn, schema, table, pk, new_rows):
    """The current (not yet updated) version of the rows an UPDATE touches, found by primary key."""
    query = sql.SQL("SELECT to_jsonb(t) FROM {s}.{t} t JOIN jsonb_populate_recordset(NULL::{s}.{t}, %s) k ON {match}").format(
        s=sql.Identifier(schema), t=sql.Identifier(table), match=_key_match(pk, "t", "k"))
    return [row[0] for row in conn.execute(query, (Jsonb(new_rows),)).fetchall()]


def _minus(effects, baseline):
    """Side effects with what a rolled-back trial run already counted taken off (the transaction's statistics keep both)."""
    done = {e["table"]: e for e in baseline}
    result = []
    for e in effects:
        b = done.get(e["table"], {})
        left = {k: e[k] - b.get(k, 0) for k in ("inserted", "updated", "deleted")}
        if any(left.values()):
            result.append({"table": e["table"], **left})
    return result


def _update_with_undo(conn, parsed, schema, table):
    """Run the UPDATE and, when it can be undone, remember the old values.

    Returns (count, sample, undo, effects). `undo` is {"pk", "old", "new", "columns"} when the old values were captured, or
    {"unavailable": reason}. Undo needs a primary key to find each row again, and at most MAX_UNDO_ROWS rows. `effects` are
    the changes the statement makes to other tables (cascades, triggers).
    """
    limit = int(settings.MAX_UNDO_ROWS)
    pk = _primary_key(conn, schema, table)
    if not pk:
        count, sample = _update_and_count(conn, parsed)
        return count, sample, {"unavailable": "the table has no primary key, so PGDAS cannot find the rows again"}, \
            _side_effects(conn, schema, table, own="updated")
    conn.execute(sql.SQL("LOCK TABLE {}.{} IN SHARE ROW EXCLUSIVE MODE").format(sql.Identifier(schema), sql.Identifier(table)))
    with conn.transaction(force_rollback=True):                      # a trial run, only to learn which rows it touches
        trial = _update_rows(conn, parsed, limit)
    trial_effects = _side_effects(conn, schema, table, own="updated")
    if len(trial) > limit:
        count, sample = _update_and_count(conn, parsed)
        return count, sample, {"unavailable": f"it changes more than {limit:,} rows, more than PGDAS keeps old values for"}, \
            _minus(_side_effects(conn, schema, table, own="updated"), trial_effects)
    old = _old_rows(conn, schema, table, pk, trial) if trial else []
    new = _update_rows(conn, parsed, limit)
    sample = new[:SAMPLE_ROWS]
    effects = _minus(_side_effects(conn, schema, table, own="updated"), trial_effects)
    if len(old) != len(new) or {_key_of(r, pk) for r in old} != {_key_of(r, pk) for r in new}:
        return len(new), sample, {"unavailable": "the statement changes the primary key, so the rows cannot be matched afterwards"}, effects
    table_columns = pg.table_columns(conn, schema, table)
    by_key = {_key_of(r, pk): r for r in old}
    changed = {c for r in new for c in r if r[c] != by_key[_key_of(r, pk)].get(c)}
    columns = [c["name"] for c in table_columns if c["name"] in changed and not c["generated"]]    # generated ones follow by themselves
    if not columns:
        return len(new), sample, {"unavailable": "it does not change any value, so there is nothing to put back"}, effects
    return len(new), sample, {"pk": pk, "old": old, "new": new, "columns": columns}, effects


def _preview_update(server, database, text):
    parsed = parse_update(text)
    with pg.connect_rw(server, database or None) as conn:
        schema, table = _resolve_table(conn, parsed["table_ref"])
        count, sample, undo, effects = _update_with_undo(conn, parsed, schema, table)
        columns, sample = _in_table_order(conn, schema, table, sample) if sample else ([], sample)
        conn.rollback()
    plural = "record" if count == 1 else "records"
    notes = []
    if not parsed["has_where"]:
        notes.append({"text": "This statement has no WHERE clause: it changes EVERY row in the table.", "bad": True})
    notes += _effect_notes(effects)
    if count > len(sample):
        notes.append({"text": f"Showing the first {len(sample)} of {count:,} rows, as they will look after the update.", "bad": False})
    undoable = "old" in undo and count
    if "unavailable" in undo and count:
        notes.append({"text": f"This UPDATE cannot be undone from the audit log: {undo['unavailable']}.", "bad": True})
    return {
        "kind": "update", "statement": parsed["statement"], "schema": schema, "table": table, "count": count,
        "has_where": parsed["has_where"], "side_effects": effects, "columns": columns, "sample": sample, "objects": [],
        "title": f"UPDATE {count:,} {plural.upper()}?" if count else "UPDATE — NOTHING TO CHANGE",
        "headline": f"{count:,}",
        "headline_label": f"will be changed in {schema}.{table}." if count else "No rows match, so there is nothing to update.",
        "warning": ("There is no rollback, but PGDAS saves the old values of these rows in the audit log, where a staff user "
                    "can undo the update (as long as the rows have not been changed again)." if undoable else
                    "There is no rollback. The old values are not saved, so the audit log cannot undo this UPDATE."),
        "notes": notes, "confirm_word": None, "button": f"Update {count:,} {plural}" if count else None,
        "fingerprint": {"count": count, "side_effects": effects, "undo": bool(undoable)},
    }


def _execute_update(server, database, text, fingerprint, user):
    parsed = parse_update(text)
    with pg.connect_rw(server, database or None) as conn:
        schema, table = _resolve_table(conn, parsed["table_ref"])
        count, _, undo, effects = _update_with_undo(conn, parsed, schema, table)
        if not count:
            raise PgError("No rows match, so there is nothing to update.")
        if count != fingerprint.get("count") or effects != fingerprint.get("side_effects") or ("old" in undo) != fingerprint.get("undo"):
            raise _changed_since(f"{fingerprint.get('count')} rows were expected and {count} match now.")
        payload = {"kind": "update", "database": _current_database(conn), "schema": schema, "table": table,
                   "statement": parsed["statement"], "count": count, "no_where": not parsed["has_where"], "side_effects": effects}
        if "old" in undo:
            _, old = _in_table_order(conn, schema, table, undo["old"])
            _, new = _in_table_order(conn, schema, table, undo["new"])
            payload["undo"] = {"pk": undo["pk"], "columns": undo["columns"], "old": old, "new": new}
        else:
            payload["undo"] = {"unavailable": undo["unavailable"]}
        entry = _commit_after_recording(conn, server, user, "update", parsed["statement"], count, payload, "updated")
    return {"count": count, "schema": schema, "table": table, "entry_id": entry.pk, "side_effects": effects,
            "message": f"Updated {count:,} row{'s' if count != 1 else ''} in {schema}.{table}. "
                       f"Recorded in the audit log as entry #{entry.pk}."}


def undo_entry(entry, user):
    """Undo a recorded UPDATE, or a saved batch of row edits from the data grid."""
    if (entry.payload or {}).get("kind") == "rows":
        from . import row_edits
        return row_edits.undo(entry, user)
    return undo_update(entry, user)


def undo_update(entry, user):
    """Put the old values of a recorded UPDATE back. All or nothing, once per record, and only if the rows still look as the
    update left them (nobody changed or deleted them since)."""
    payload = entry.payload or {}
    undo = payload.get("undo") or {}
    if payload.get("kind") != "update" or not entry.ok:
        raise PgError("This audit entry is not a recorded UPDATE.")
    if entry.restored_at is not None:
        raise PgError("This update was already undone.")
    if "old" not in undo or not undo["old"]:
        raise PgError(f"This update cannot be undone: {undo.get('unavailable', 'its old values were not saved')}.")
    server = entry.server
    if server is None:
        raise PgError("The server this was updated on is no longer saved in PGDAS.")
    schema, table, pk = payload["schema"], payload["table"], undo["pk"]
    with pg.connect_rw(server, payload["database"]) as conn:
        _resolve_table(conn, _qualified(conn, schema, table))
        if _primary_key(conn, schema, table) != pk:
            raise PgError("The primary key of the table is not what it was, so the rows cannot be matched. Nothing was changed.")
        conn.execute(sql.SQL("LOCK TABLE {}.{} IN SHARE ROW EXCLUSIVE MODE").format(sql.Identifier(schema), sql.Identifier(table)))
        still = sql.SQL("SELECT count(*) FROM {s}.{t} t JOIN jsonb_populate_recordset(NULL::{s}.{t}, %s) n ON {match} "
                        "WHERE to_jsonb(t) = to_jsonb(n)").format(s=sql.Identifier(schema), t=sql.Identifier(table), match=_key_match(pk, "t", "n"))
        unchanged = conn.execute(still, (Jsonb(undo["new"]),)).fetchone()[0]
        if unchanged != len(undo["new"]):
            raise PgError(f"{len(undo['new']) - unchanged} of the {len(undo['new']):,} rows were changed or deleted again after this "
                          "update, so nothing was undone. Undoing now would overwrite the newer values.")
        columns = [c for c in undo["columns"] if c]
        assignments = sql.SQL(", ").join(sql.SQL("{} = o.{}").format(sql.Identifier(c), sql.Identifier(c)) for c in columns)
        restore = sql.SQL("UPDATE {s}.{t} t SET {assign} FROM jsonb_populate_recordset(NULL::{s}.{t}, %s) o WHERE {match}").format(
            s=sql.Identifier(schema), t=sql.Identifier(table), assign=assignments, match=_key_match(pk, "t", "o"))
        done = conn.execute(restore, (Jsonb(undo["old"]),)).rowcount
        if done != len(undo["old"]):
            raise PgError(f"Only {done} of {len(undo['old'])} rows could be put back, so nothing was changed.")
        entry.restored_at, entry.restored_by = timezone.now(), user.get_username()
        entry.save(update_fields=["restored_at", "restored_by"])
        undo_entry = record(user, "update.undo", server, f"Undid {done} updated rows in {schema}.{table} (audit entry #{entry.pk})",
                            rows=done, payload={"kind": "undo", "undone_entry": entry.pk, "database": payload["database"],
                                                "schema": schema, "table": table, "count": done, "columns": columns})
        try:
            conn.commit()
        except Exception:
            entry.restored_at, entry.restored_by = None, ""
            entry.save(update_fields=["restored_at", "restored_by"])
            undo_entry.delete()
            raise PgError("The database did not accept the undo, so nothing was changed.")
    return {"count": done, "schema": schema, "table": table}


# ------------------------------------------------------------------------------------------------------------- INSERT
_INSERT = re.compile(rf"^INSERT\s+INTO\s+(?P<table>{_TABLE_REF})", re.I)


def parse_insert(text):
    cleaned, _ = prepare_statement(text, "INSERT", allow_returning=True)
    match = _INSERT.match(cleaned)
    if not match:
        raise PgError("Write it as INSERT INTO table (columns) VALUES (…) or INSERT INTO table SELECT …")
    return {"statement": cleaned, "table_ref": _normalise(match["table"])}


def _execute_insert(server, database, text, user):
    """No popup: an INSERT only adds rows. It is recorded in the audit log like everything else."""
    parsed = parse_insert(text)
    with pg.connect_rw(server, database or None) as conn:
        schema, table = _resolve_table(conn, parsed["table_ref"])
        cursor = _run(conn, parsed["statement"])
        inserted = cursor.rowcount
        returned = None
        if cursor.description:                                   # INSERT … RETURNING: show what came back, capped
            limit = int(settings.MAX_ROWS)
            rows = cursor.fetchmany(limit + 1)
            returned = {"columns": [c.name for c in cursor.description], "truncated": len(rows) > limit,
                        "rows": [[pg.display(value) for value in row] for row in rows[:limit]]}
        effects = _side_effects(conn, schema, table, own="inserted")
        payload = {"kind": "insert", "database": _current_database(conn), "schema": schema, "table": table,
                   "statement": parsed["statement"], "count": inserted, "side_effects": effects}
        entry = _commit_after_recording(conn, server, user, "insert", parsed["statement"], inserted, payload, "inserted")
    return {"count": inserted, "schema": schema, "table": table, "entry_id": entry.pk, "side_effects": effects,
            "returned": returned,
            "message": f"Inserted {inserted:,} row{'s' if inserted != 1 else ''} into {schema}.{table}. "
                       f"Recorded in the audit log as entry #{entry.pk}."}


# ---------------------------------------------------------------------------------------------------------- TRUNCATE
_TRUNCATE = re.compile(r"^TRUNCATE(?:\s+TABLE)?\s+(?P<body>.+?)"
                       r"(?P<opts>(?:\s+(?:RESTART\s+IDENTITY|CONTINUE\s+IDENTITY|CASCADE|RESTRICT))*)\s*$", re.I | re.S)
_TRUNCATE_ITEM = re.compile(rf"^(?:ONLY\s+)?(?P<name>{_TABLE_REF})(?:\s*\*)?$", re.I)


def parse_truncate(text):
    cleaned, _ = prepare_statement(text, "TRUNCATE", allow_returning=True)
    usage = "Write it as TRUNCATE [TABLE] name [, name …] [RESTART IDENTITY] [CASCADE]."
    match = _TRUNCATE.match(cleaned)
    if not match:
        raise PgError(usage)
    names = []
    for part in match["body"].split(","):
        item = _TRUNCATE_ITEM.match(part.strip())
        if not item:
            raise PgError(usage)
        names.append(_normalise(item["name"]))
    options = match["opts"].upper()
    return {"statement": cleaned, "tables": names, "cascade": "CASCADE" in options, "restart_identity": "RESTART" in options}


def _truncate_facts(conn, parsed):
    """Every table the statement empties and how many rows each holds now. With CASCADE that includes tables that point at them."""
    named = list(dict.fromkeys(_resolve_table(conn, ref) for ref in parsed["tables"]))
    facts = [{"name": f"{s}.{t}", "rows": _count(conn, s, t), "cascade": False} for s, t in named]
    if parsed["cascade"]:
        dependents = conn.execute(
            """WITH RECURSIVE refs(oid) AS (
                   SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE (n.nspname, c.relname) IN (SELECT a, b FROM unnest(%s::text[], %s::text[]) AS x(a, b))
                   UNION
                   SELECT k.conrelid FROM pg_constraint k JOIN refs r ON k.confrelid = r.oid WHERE k.contype = 'f')
               SELECT n.nspname, c.relname FROM refs JOIN pg_class c ON c.oid = refs.oid
               JOIN pg_namespace n ON n.oid = c.relnamespace ORDER BY 1, 2""",
            ([s for s, _ in named], [t for _, t in named])).fetchall()
        facts += [{"name": f"{s}.{t}", "rows": _count(conn, s, t), "cascade": True} for s, t in dependents if (s, t) not in named]
    return facts


def _preview_truncate(server, database, text):
    parsed = parse_truncate(text)
    with pg.connect_rw(server, database or None) as conn:
        facts = _truncate_facts(conn, parsed)
        _run(conn, parsed["statement"])                          # proves it would work (and shows foreign-key problems)
        conn.rollback()
    total = sum(f["rows"] for f in facts)
    notes = []
    also = [f for f in facts if f["cascade"]]
    if also:
        notes.append({"text": "CASCADE also empties: " + ", ".join(f"{f['name']} ({f['rows']:,} rows)" for f in also) + ".", "bad": True})
    if parsed["restart_identity"]:
        notes.append({"text": "RESTART IDENTITY also resets the numbering of the tables' sequences.", "bad": False})
    word = "TABLE" if len(facts) == 1 else "TABLES"
    return {
        "kind": "truncate", "statement": parsed["statement"], "count": total, "objects": facts, "notes": notes,
        "title": f"TRUNCATE {len(facts)} {word}?", "headline": f"{total:,}",
        "headline_label": f"row{'s' if total != 1 else ''} will be removed from {len(facts)} table{'s' if len(facts) != 1 else ''} at once.",
        "warning": "TRUNCATE empties the table at once and cannot be rolled back. Unlike DELETE, the rows are NOT saved in the "
                   "audit log, so they cannot be re-inserted from there.",
        "confirm_word": CONFIRM_WORDS["truncate"], "button": f"Truncate {len(facts)} table{'s' if len(facts) != 1 else ''}",
        "side_effects": [], "columns": [], "sample": [], "has_where": True,
        "fingerprint": {"tables": facts},
    }


def _execute_truncate(server, database, text, fingerprint, confirm, user):
    if confirm != CONFIRM_WORDS["truncate"]:
        raise PgError("Type TRUNCATE in capital letters to confirm.")
    parsed = parse_truncate(text)
    with pg.connect_rw(server, database or None) as conn:
        facts = _truncate_facts(conn, parsed)
        if facts != fingerprint.get("tables"):
            raise _changed_since("the tables or their row counts are not what you were shown.")
        _run(conn, parsed["statement"])
        total = sum(f["rows"] for f in facts)
        payload = {"kind": "truncate", "database": _current_database(conn), "statement": parsed["statement"],
                   "cascade": parsed["cascade"], "restart_identity": parsed["restart_identity"], "total_rows": total, "tables": facts}
        entry = _commit_after_recording(conn, server, user, "truncate", parsed["statement"], total, payload, "truncated")
    return {"count": total, "entry_id": entry.pk, "tables": facts,
            "message": f"Emptied {len(facts)} table{'s' if len(facts) != 1 else ''} ({total:,} row{'s' if total != 1 else ''}). "
                       f"Recorded in the audit log as entry #{entry.pk}."}


# ------------------------------------------------------------------------------------------------------------- DROP
_DROP = re.compile(r"^DROP\s+(?P<type>MATERIALIZED\s+VIEW|FOREIGN\s+TABLE|EVENT\s+TRIGGER|TABLE|VIEW|INDEX|SEQUENCE|SCHEMA|"
                   r"FUNCTION|PROCEDURE|TYPE|DOMAIN|TRIGGER|EXTENSION|PUBLICATION)\s+(?P<rest>.+)$", re.I | re.S)
_DROP_REFUSED = re.compile(r"^DROP\s+(DATABASE|ROLE|USER|GROUP|TABLESPACE|OWNED)\b", re.I)
_PLAIN_TABLE_NAME = re.compile(rf"^{_TABLE_REF}$")


def parse_drop(text):
    cleaned, _ = prepare_statement(text, "DROP", allow_returning=True)
    if _DROP_REFUSED.match(cleaned):
        raise PgError("DROP TABLESPACE and DROP OWNED are not available here; run those in psql.")
    match = _DROP.match(cleaned)
    if not match:
        raise PgError("PGDAS can drop tables, views, materialized views, indexes, sequences, schemas, functions, procedures, "
                      "types, domains, triggers, extensions and publications.")
    object_type = re.sub(r"\s+", " ", match["type"]).upper()
    rest = re.sub(r"^\s*IF\s+EXISTS\s+", "", match["rest"], flags=re.I)
    cascade = bool(re.search(r"\s+CASCADE\s*$", rest, re.I))
    rest = re.sub(r"\s+(CASCADE|RESTRICT)\s*$", "", rest, flags=re.I)
    names = []
    if object_type in ("TABLE", "MATERIALIZED VIEW"):            # for these PGDAS shows how many rows go with them
        parts = [p.strip() for p in rest.split(",") if p.strip()]
        names = [_normalise(p) for p in parts] if all(_PLAIN_TABLE_NAME.match(p) for p in parts) else []
    return {"statement": cleaned, "object_type": object_type, "names": names, "cascade": cascade}


def _drop_and_collect(conn, parsed):
    """Run the DROP, noting what it would take with it. Returns (tables with row counts, notices, CREATE statements)."""
    tables, ddl = [], []
    for ref in parsed["names"]:
        row = conn.execute(
            """SELECT n.nspname, c.relname, c.relkind::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE c.oid = to_regclass(%s)""", (ref,)).fetchone()
        if row and row[2] in ("r", "p", "m"):
            schema, table, relkind = row
            tables.append({"name": f"{schema}.{table}", "rows": _count(conn, schema, table)})
            try:
                ddl.append({"name": f"{schema}.{table}",
                            "sql": pg.view_ddl(conn, schema, table) if relkind == "m" else pg.table_ddl(conn, schema, table)})
            except PgError:
                pass                                              # a definition we cannot rebuild is simply not saved
    notices = []

    def note(diagnostic):
        text = " ".join(part.strip() for part in (diagnostic.message_primary, diagnostic.message_detail) if part)
        notices.append(text)

    conn.add_notice_handler(note)
    try:
        _run(conn, parsed["statement"])
    finally:
        conn.remove_notice_handler(note)
    return tables, notices, ddl


def _preview_drop(server, database, text):
    parsed = parse_drop(text)
    with pg.connect_rw(server, database or None) as conn:
        tables, notices, _ = _drop_and_collect(conn, parsed)
        conn.rollback()
    total = sum(t["rows"] for t in tables)
    kind_name = parsed["object_type"]
    notes = [{"text": notice, "bad": "cascades" in notice} for notice in notices]
    if tables:
        headline, label = f"{total:,}", f"row{'s' if total != 1 else ''} will be lost with the dropped table{'s' if len(tables) != 1 else ''}."
    else:
        headline, label = kind_name, "will be permanently removed."
    return {
        "kind": "drop", "statement": parsed["statement"], "count": total, "objects": [{**t, "cascade": False} for t in tables],
        "notes": notes, "title": f"DROP {kind_name}?", "headline": headline, "headline_label": label,
        "warning": "DROP permanently removes the object and everything stored in it. There is no rollback, and the audit log cannot "
                   "bring it back: for tables and materialized views it keeps the CREATE statement for reference, not the rows.",
        "confirm_word": CONFIRM_WORDS["drop"], "button": f"Drop {kind_name.lower()}",
        "side_effects": [], "columns": [], "sample": [], "has_where": True,
        "fingerprint": {"tables": tables, "notices": notices},
    }


def _execute_drop(server, database, text, fingerprint, confirm, user):
    if confirm != CONFIRM_WORDS["drop"]:
        raise PgError("Type DROP in capital letters to confirm.")
    parsed = parse_drop(text)
    with pg.connect_rw(server, database or None) as conn:
        tables, notices, ddl = _drop_and_collect(conn, parsed)
        if tables != fingerprint.get("tables") or notices != fingerprint.get("notices"):
            raise _changed_since("what depends on this object, or its row counts, is not what you were shown.")
        total = sum(t["rows"] for t in tables)
        payload = {"kind": "drop", "database": _current_database(conn), "object_type": parsed["object_type"],
                   "statement": parsed["statement"], "cascade": parsed["cascade"], "total_rows": total, "tables": tables,
                   "notices": notices, "ddl": ddl}
        entry = _commit_after_recording(conn, server, user, "drop", parsed["statement"], total, payload, "dropped")
    return {"count": total, "entry_id": entry.pk, "notices": notices,
            "message": f"Dropped as asked ({parsed['object_type'].lower()}). Recorded in the audit log as entry #{entry.pk}."}


# ------------------------------------------------------------------------------------------ CREATE and ALTER
# PGDAS does not try to understand every DDL statement. It runs the statement inside a transaction, looks at the database
# before and after, and reports the difference: that is exactly what the popup shows, and what must still be true when the
# person confirms.
_OBJECT_TYPES = (r"MATERIALIZED\s+VIEW|FOREIGN\s+TABLE|EVENT\s+TRIGGER|TABLE|VIEW|INDEX|SEQUENCE|SCHEMA|TYPE|DOMAIN|"
                 r"FUNCTION|PROCEDURE|TRIGGER|EXTENSION|PUBLICATION")
_CREATE = re.compile(rf"^CREATE\s+(?P<mods>(?:(?:OR\s+REPLACE|UNIQUE|TEMP(?:ORARY)?|UNLOGGED|GLOBAL|LOCAL|RECURSIVE)\s+)*)"
                     rf"(?P<type>{_OBJECT_TYPES})\b(?P<rest>.*)$", re.I | re.S)
_ALTER = re.compile(rf"^ALTER\s+(?P<type>{_OBJECT_TYPES})\b(?P<rest>.*)$", re.I | re.S)
_CREATE_REFUSED = re.compile(r"^CREATE\s+(?:OR\s+REPLACE\s+)?(DATABASE|ROLE|USER|GROUP|TABLESPACE|SERVER|FOREIGN\s+DATA|"
                             r"USER\s+MAPPING|LANGUAGE|CAST|COLLATION|CONVERSION|OPERATOR|AGGREGATE|ACCESS\s+METHOD)\b", re.I)
_ALTER_REFUSED = re.compile(r"^ALTER\s+(SYSTEM|DATABASE|ROLE|USER|GROUP|TABLESPACE|DEFAULT\s+PRIVILEGES|SERVER|"
                            r"FOREIGN\s+DATA|USER\s+MAPPING|LARGE\s+OBJECT|OPERATOR|AGGREGATE)\b", re.I)
_NAMED_TARGET = re.compile(rf"^(?:IF\s+(?:NOT\s+)?EXISTS\s+)?(?:ONLY\s+)?(?P<name>{_TABLE_REF})", re.I)
_QUOTED = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|\$(\w*)\$.*?\$\1\$", re.S)
_RISKY_ALTER = re.compile(r"\b(DROP|RENAME|TYPE|OWNER\s+TO|SET\s+SCHEMA|DETACH|DISABLE|RESTART|CASCADE|SET\s+(?:LOGGED|UNLOGGED))\b", re.I)


def _target_names(object_type, rest):
    """For tables and views: the name being created or altered, so its current definition can be kept in the audit record."""
    if object_type not in ("TABLE", "VIEW", "MATERIALIZED VIEW"):
        return []
    match = _NAMED_TARGET.match(rest.strip())
    return [_normalise(match["name"])] if match else []


def parse_create(text):
    cleaned, _ = prepare_statement(text, "CREATE", allow_returning=True)
    if _CREATE_REFUSED.match(cleaned):
        raise PgError("CREATE TABLESPACE, SERVER and similar server-level objects are not available here; run those in psql.")
    match = _CREATE.match(cleaned)
    if not match:
        raise PgError("PGDAS can create tables, views, materialized views, indexes, sequences, schemas, types, domains, "
                      "functions, procedures, triggers, extensions and publications.")
    mods = re.sub(r"\s+", " ", match["mods"]).upper()
    object_type = re.sub(r"\s+", " ", match["type"]).upper()
    if "TEMP" in mods:
        raise PgError("Temporary objects disappear when the connection closes, so there is nothing to keep. Create a normal one instead.")
    if re.search(r"\bCONCURRENTLY\b", _QUOTED.sub(" ", cleaned), re.I):
        raise PgError("CREATE INDEX CONCURRENTLY cannot run inside PGDAS's safety transaction. Create the index without "
                      "CONCURRENTLY, or run it in psql.")
    return {"statement": cleaned, "object_type": object_type, "or_replace": "OR REPLACE" in mods,
            "names": _target_names(object_type, match["rest"])}


def parse_alter(text):
    cleaned, _ = prepare_statement(text, "ALTER", allow_returning=True)
    if _ALTER_REFUSED.match(cleaned):
        raise PgError("ALTER SYSTEM, TABLESPACE and similar server-level settings are not available here; run those in psql.")
    match = _ALTER.match(cleaned)
    if not match:
        raise PgError("PGDAS can alter tables, indexes, sequences, views, materialized views, schemas, types, domains, functions, "
                      "procedures, triggers, extensions and publications.")
    object_type = re.sub(r"\s+", " ", match["type"]).upper()
    return {"statement": cleaned, "object_type": object_type, "names": _target_names(object_type, match["rest"]),
            "risky_words": _alter_risky_words(match["rest"])}


def _alter_risky_words(rest):
    """Words in the statement that mean data can be lost or things that depend on the object can break."""
    return sorted({re.sub(r"\s+", " ", word).upper() for word in _RISKY_ALTER.findall(_QUOTED.sub(" ", rest))})


# What the database looks like, as {(kind, name): fingerprint}. Only the database's own schemas, not system or extension objects.
_USER_SCHEMA = "n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'"
_SNAPSHOT = f"""
SELECT 'relation', n.nspname || '.' || c.relname,
       c.relkind::text || E'\\x1d' || pg_get_userbyid(c.relowner) || E'\\x1d' || coalesce((
           SELECT string_agg(a.attname || E'\\x1e' || format_type(a.atttypid, a.atttypmod)
                             || CASE WHEN a.attnotnull THEN ' NOT NULL' ELSE '' END
                             || coalesce(' DEFAULT ' || pg_get_expr(d.adbin, d.adrelid), '')
                             || CASE WHEN a.attidentity <> '' THEN ' IDENTITY ' || a.attidentity::text ELSE '' END
                             || CASE WHEN a.attgenerated <> '' THEN ' GENERATED ' || a.attgenerated::text ELSE '' END,
                             E'\\x1f' ORDER BY a.attnum)
           FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
           WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped), '')
       || E'\\x1d' || CASE WHEN c.relkind IN ('v', 'm') THEN md5(pg_get_viewdef(c.oid, true)) ELSE '' END
       || E'\\x1d' || coalesce((SELECT concat_ws(',', s.seqincrement, s.seqmin, s.seqmax, s.seqstart, s.seqcycle)
                                FROM pg_sequence s WHERE s.seqrelid = c.oid), '')
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f') AND {_USER_SCHEMA} AND {pg.NOT_IN_EXTENSION.format(oid="c.oid", catalog="pg_class")}
UNION ALL
SELECT 'index', n.nspname || '.' || i.relname, pg_get_indexdef(i.oid)
FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid JOIN pg_class c ON c.oid = x.indrelid JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE {_USER_SCHEMA} AND {pg.NOT_IN_EXTENSION.format(oid="c.oid", catalog="pg_class")}
UNION ALL
SELECT 'constraint', n.nspname || '.' || c.relname || '.' || k.conname, pg_get_constraintdef(k.oid)
FROM pg_constraint k JOIN pg_class c ON c.oid = k.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE k.contype <> 'n' AND {_USER_SCHEMA} AND {pg.NOT_IN_EXTENSION.format(oid="c.oid", catalog="pg_class")}
UNION ALL
SELECT 'trigger', n.nspname || '.' || c.relname || '.' || t.tgname, pg_get_triggerdef(t.oid)
FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE NOT t.tgisinternal AND {_USER_SCHEMA} AND {pg.NOT_IN_EXTENSION.format(oid="c.oid", catalog="pg_class")}
UNION ALL
SELECT CASE p.prokind WHEN 'p' THEN 'procedure' WHEN 'a' THEN 'aggregate' ELSE 'function' END,
       n.nspname || '.' || p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')',
       CASE WHEN p.prokind = 'a' THEN 'aggregate' ELSE md5(pg_get_functiondef(p.oid)) END
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE {_USER_SCHEMA} AND {pg.NOT_IN_EXTENSION.format(oid="p.oid", catalog="pg_proc")}
UNION ALL
SELECT 'type', n.nspname || '.' || t.typname,
       t.typtype::text || ':' || coalesce((SELECT string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder) FROM pg_enum e WHERE e.enumtypid = t.oid), '')
       || ':' || coalesce(format_type(nullif(t.typbasetype, 0), t.typtypmod), '')
       || ':' || coalesce((SELECT string_agg(a.attname || ' ' || format_type(a.atttypid, a.atttypmod), ',' ORDER BY a.attnum)
                           FROM pg_attribute a WHERE a.attrelid = nullif(t.typrelid, 0) AND a.attnum > 0 AND NOT a.attisdropped), '')
FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace LEFT JOIN pg_class rel ON rel.oid = t.typrelid
WHERE t.typtype IN ('e', 'd', 'r', 'c') AND (t.typtype <> 'c' OR rel.relkind = 'c') AND {_USER_SCHEMA}
      AND {pg.NOT_IN_EXTENSION.format(oid="t.oid", catalog="pg_type")}
UNION ALL
SELECT 'schema', n.nspname, pg_get_userbyid(n.nspowner) FROM pg_namespace n WHERE {_USER_SCHEMA}
UNION ALL
SELECT 'extension', e.extname, e.extversion FROM pg_extension e
UNION ALL
SELECT 'publication', p.pubname, p.puballtables::text || p.pubinsert::text || p.pubupdate::text || p.pubdelete::text FROM pg_publication p
"""
_RELATION_KIND = {"r": "table", "p": "partitioned table", "v": "view", "m": "materialized view", "S": "sequence", "f": "foreign table"}


def _snapshot(conn):
    return {(kind, name): fingerprint for kind, name, fingerprint in _run(conn, _SNAPSHOT).fetchall()}


def _label(kind, fingerprint):
    return _RELATION_KIND.get(fingerprint.split("\x1d", 1)[0], kind) if kind == "relation" else kind


def _describe_relation_change(before, after):
    """'columns: +email, −age, ~name' (and what else changed) for a table or view whose definition differs."""
    old, new = before.split("\x1d"), after.split("\x1d")
    columns = lambda fp: dict(pair.split("\x1e", 1) for pair in fp.split("\x1f") if "\x1e" in pair)
    old_columns, new_columns = columns(old[2]), columns(new[2])
    parts = []
    added = [name for name in new_columns if name not in old_columns]
    removed = [name for name in old_columns if name not in new_columns]
    changed = [name for name in new_columns if name in old_columns and new_columns[name] != old_columns[name]]
    if added or removed or changed:
        parts.append("columns: " + ", ".join([f"+{n}" for n in added] + [f"−{n}" for n in removed] + [f"~{n}" for n in changed]))
    if old[0] != new[0]:
        parts.append(f"now a {_RELATION_KIND.get(new[0], 'relation')}")
    if old[1] != new[1]:
        parts.append(f"owner {old[1]} → {new[1]}")
    if old[3] != new[3]:
        parts.append("definition changed")
    if old[4] != new[4]:
        parts.append("sequence settings changed")
    return "; ".join(parts) or "changed"


_ORDER = {"schema": 0, "extension": 0, "relation": 1, "type": 1, "function": 1, "procedure": 1, "aggregate": 1, "publication": 1,
          "index": 2, "constraint": 2, "trigger": 2}


def _diff(before, after, conn):
    """What a statement did, as [{"action": create|alter|drop, "kind", "name", "detail", "rows"}], sorted for reading."""
    changes = []
    for key in sorted(set(before) | set(after)):
        kind, name = key
        if key not in before:
            entry = {"action": "create", "kind": _label(kind, after[key]), "name": name, "detail": "", "rows": None}
            if kind == "relation" and after[key].split("\x1d", 1)[0] in ("r", "p", "m"):
                schema, table = name.split(".", 1)
                entry["rows"] = _count(conn, schema, table)
        elif key not in after:
            entry = {"action": "drop", "kind": _label(kind, before[key]), "name": name, "detail": "", "rows": None}
        elif before[key] != after[key]:
            detail = _describe_relation_change(before[key], after[key]) if kind == "relation" else "definition changed"
            entry = {"action": "alter", "kind": _label(kind, after[key]), "name": name, "detail": detail, "rows": None}
        else:
            continue
        changes.append((_ORDER.get(kind, 3), entry))
    return [entry for _, entry in sorted(changes, key=lambda pair: (pair[0], pair[1]["name"]))]


def _definitions_before(conn, names):
    """The current CREATE statement of the tables and views about to be changed, kept in the audit record."""
    found = []
    for ref in names:
        row = conn.execute("SELECT n.nspname, c.relname, c.relkind::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                           "WHERE c.oid = to_regclass(%s)", (ref,)).fetchone()
        if row and row[2] in ("r", "p", "v", "m"):
            schema, table, relkind = row
            try:
                found.append({"name": f"{schema}.{table}",
                              "sql": pg.view_ddl(conn, schema, table) if relkind in ("v", "m") else pg.table_ddl(conn, schema, table)})
            except PgError:
                pass
    return found


def _ddl_and_diff(conn, parsed):
    """Run the CREATE/ALTER and return (what changed, the definitions it replaced)."""
    before_definitions = _definitions_before(conn, parsed["names"])
    before = _snapshot(conn)
    _run(conn, parsed["statement"])
    return _diff(before, _snapshot(conn), conn), before_definitions


def _alter_is_risky(parsed, changes):
    """Typing ALTER is required when the change can lose data or break things that depend on the object."""
    return bool(parsed["risky_words"]) or any(c["action"] == "drop" for c in changes)


def _change_lines(changes):
    marks = {"create": "+ create", "alter": "~ change", "drop": "− drop"}
    lines = []
    for c in changes:
        text = f"{marks[c['action']]} {c['kind']} {c['name']}"
        if c["rows"] is not None:
            text += f" ({c['rows']:,} row{'s' if c['rows'] != 1 else ''})"
        if c["detail"]:
            text += f" — {c['detail']}"
        lines.append({"name": c["name"], "text": text, "rows": c["rows"], "cascade": c["action"] == "drop"})
    return lines


def _main_names(changes):
    names = [c["name"] for c in changes if c["kind"] not in ("index", "constraint", "trigger")]
    return names[:3], len(names)


def _preview_ddl(server, database, text, verb):
    parsed = parse_create(text) if verb == "create" else parse_alter(text)
    with pg.connect_rw(server, database or None) as conn:
        changes, _ = _ddl_and_diff(conn, parsed)
        conn.rollback()
    object_type = parsed["object_type"]
    notes, risky = [], False
    drops = [c for c in changes if c["action"] == "drop"]
    if verb == "alter":
        risky = _alter_is_risky(parsed, changes)
        if parsed["risky_words"]:
            notes.append({"text": "Risky because of: " + ", ".join(parsed["risky_words"]) + ".", "bad": True})
        if parsed["object_type"] in ("TABLE", "FOREIGN TABLE"):
            notes.append({"text": "ALTER TABLE locks the table while it runs; on a big table that can block other work.", "bad": False})
    else:
        replaced = [c for c in changes if c["action"] == "alter"]
        if parsed["or_replace"] and replaced:
            notes.append({"text": "OR REPLACE overwrites the existing definition of " + ", ".join(c["name"] for c in replaced) +
                                  ". The old definition is kept in the audit log for views, not for functions.", "bad": True})
        loaded = [c for c in changes if c["action"] == "create" and c["rows"]]
        for c in loaded:
            notes.append({"text": f"{c['name']} is filled with {c['rows']:,} row{'s' if c['rows'] != 1 else ''} from the query.", "bad": False})
    for c in drops:
        notes.append({"text": f"This also drops {c['kind']} {c['name']}.", "bad": True})
    names, total = _main_names(changes)
    if not changes:
        title, headline, label = f"{verb.upper()} — NOTHING TO DO", object_type, \
            "Nothing would change: the object may already exist, or the statement does nothing."
        button, word = None, None
    else:
        title = f"{verb.upper()} {object_type}?"
        headline = object_type
        label = (", ".join(names) + (f" and {total - len(names)} more" if total > len(names) else "")
                 + (" will be created." if verb == "create" else " will be changed.")) if names else "will change the database."
        button = f"{verb.capitalize()} {object_type.lower()}"
        word = CONFIRM_WORDS["alter"] if risky else None
    if verb == "create":
        warning = "CREATE adds new objects. It cannot be rolled back, but a new object can be removed again with DROP."
    elif risky:
        warning = ("This ALTER can lose data or break things that depend on the object. There is no rollback: the audit log keeps "
                   "the previous CREATE statement of a table or view, not its data.")
    else:
        warning = "ALTER changes the structure of the object. There is no rollback, but it can usually be changed back with another ALTER."
    return {"kind": verb, "statement": parsed["statement"], "count": len(changes), "objects": _change_lines(changes), "notes": notes,
            "title": title, "headline": headline, "headline_label": label, "warning": warning, "confirm_word": word, "button": button,
            "side_effects": [], "columns": [], "sample": [], "has_where": True, "risky": risky,
            "fingerprint": {"changes": changes}}


def _execute_ddl(server, database, text, fingerprint, confirm, user, verb):
    parsed = parse_create(text) if verb == "create" else parse_alter(text)
    with pg.connect_rw(server, database or None) as conn:
        changes, before_definitions = _ddl_and_diff(conn, parsed)
        if not changes:
            raise PgError("Nothing would change, so there is nothing to run.")
        if changes != fingerprint.get("changes"):
            raise _changed_since("the objects this statement would create or change are not what you were shown.")
        risky = verb == "alter" and _alter_is_risky(parsed, changes)
        if risky and confirm != CONFIRM_WORDS["alter"]:
            raise PgError("Type ALTER in capital letters to confirm.")
        payload = {"kind": verb, "database": _current_database(conn), "object_type": parsed["object_type"],
                   "statement": parsed["statement"], "risky": risky, "changes": changes, "before": before_definitions}
        entry = _commit_after_recording(conn, server, user, verb, parsed["statement"], len(changes), payload, "changed")
    return {"count": len(changes), "entry_id": entry.pk, "changes": changes,
            "message": f"{verb.capitalize()} done: {len(changes)} object{'s' if len(changes) != 1 else ''} "
                       f"{'created' if verb == 'create' else 'changed'}. Recorded in the audit log as entry #{entry.pk}."}


# --------------------------------------------------------------------------------------------------- entry points
def _effect_notes(effects):
    notes = []
    for effect in effects:
        parts = [f"{effect[key]} {word}" for key, word in (("deleted", "deleted"), ("updated", "updated"), ("inserted", "inserted")) if effect[key]]
        notes.append({"text": f"It also changes {effect['table']} ({', '.join(parts)}) through cascades or triggers. "
                              "Those changes are not saved in the audit log.", "bad": True})
    return notes


def _preview_delete(server, database, text):
    data = writes.preview_delete(server, database, text)
    count, plural = data["count"], "record" if data["count"] == 1 else "records"
    notes = []
    if not data["has_where"]:
        notes.append({"text": "This statement has no WHERE clause: it deletes EVERY row in the table.", "bad": True})
    for effect in data["side_effects"]:
        parts = [f"{effect[key]} {word}" for key, word in (("deleted", "deleted"), ("updated", "updated"), ("inserted", "inserted")) if effect[key]]
        notes.append({"text": f"It also changes {effect['table']} ({', '.join(parts)}) through cascades or triggers. Those rows are not "
                              "saved in the audit log and cannot be re-inserted from it.", "bad": True})
    if count > len(data["sample"]):
        notes.append({"text": f"Showing the first {len(data['sample'])} of {count:,} rows. All of them are saved in the audit log.", "bad": False})
    return {**data, "kind": "delete", "objects": [], "notes": notes,
            "title": f"DELETE {count:,} {plural.upper()}?" if count else "DELETE — NOTHING TO DELETE",
            "headline": f"{count:,}",
            "headline_label": f"will be permanently deleted from {data['schema']}.{data['table']}." if count
                              else "No rows match, so there is nothing to delete.",
            "warning": "There is no rollback. Once you confirm, the rows are deleted for good. PGDAS saves a copy of every deleted "
                       "row in the audit log (as JSON), where a staff user can re-insert them, but nothing else is undone.",
            "confirm_word": CONFIRM_WORDS["delete"] if count else None,
            "button": f"Delete {count:,} {plural}" if count else None,
            "fingerprint": {"count": count, "side_effects": data["side_effects"]}}


def preview(server, database, text):
    """What a statement would do, without doing it. Returns everything the warning popup shows."""
    kind = classify(text)
    if kind == "insert":
        raise PgError("INSERT runs straight away, so there is nothing to preview.")
    from . import server_admin                      # imported here because server_admin builds on this module
    if server_admin.is_admin_statement(text):
        return server_admin.preview(server, database, text)
    if kind in ("grant", "revoke"):
        raise PgError("Write it as GRANT … TO … or REVOKE … FROM ….")
    from . import table_rebuild
    if kind == "alter" and table_rebuild.is_reorder_statement(text):
        return table_rebuild.preview(server, database, text)
    if kind in ("create", "alter"):
        return _preview_ddl(server, database, text, kind)
    return {"delete": _preview_delete, "update": _preview_update, "truncate": _preview_truncate, "drop": _preview_drop}[kind](
        server, database, text)


def execute(server, database, text, fingerprint, confirm, user):
    """Run a changing statement for real. `fingerprint` is what preview() reported; `confirm` is the word that was typed."""
    kind = classify(text)
    fingerprint = fingerprint if isinstance(fingerprint, dict) else {}
    from . import server_admin
    if server_admin.is_admin_statement(text):
        result = server_admin.execute(server, database, text, fingerprint, confirm, user)
        return {**result, "kind": kind}
    from . import table_rebuild
    if kind == "alter" and table_rebuild.is_reorder_statement(text):
        return {"kind": kind, **table_rebuild.execute(server, database, text, fingerprint, confirm, user)}
    if kind == "insert":
        result = _execute_insert(server, database, text, user)
    elif kind == "update":
        result = _execute_update(server, database, text, fingerprint, user)
    elif kind == "truncate":
        result = _execute_truncate(server, database, text, fingerprint, confirm, user)
    elif kind == "drop":
        result = _execute_drop(server, database, text, fingerprint, confirm, user)
    elif kind in ("create", "alter"):
        result = _execute_ddl(server, database, text, fingerprint, confirm, user, kind)
    else:
        count = fingerprint.get("count")
        if not isinstance(count, int):
            raise PgError("That request was not understood.")
        done = writes.execute_delete(server, database, text, count, fingerprint.get("side_effects") or [], confirm, user)
        result = {**done, "message": f"Deleted {done['count']:,} row{'s' if done['count'] != 1 else ''} from {done['schema']}.{done['table']}. "
                                     f"Saved in the audit log as entry #{done['entry_id']}, where it can be re-inserted."}
    return {"kind": kind, **result}
