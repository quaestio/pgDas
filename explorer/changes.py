"""INSERT, UPDATE, TRUNCATE and DROP, and the one entry point (preview / execute) the SQL runner uses for every changing
statement. DELETE, with its row-by-row JSON record and re-insert, lives in writes.py.

  statement   what happens                                      confirmation
  INSERT      runs at once, recorded in the audit log           none
  UPDATE      popup: number of rows, warning if no WHERE        a button
  DELETE      popup: number of rows, no rollback                type ZAP
  TRUNCATE    popup: rows lost per table, no rollback           type TRUNCATE
  DROP        popup: what goes with it, no rollback             type DROP

Every one of them: needs a staff user and the server's own switch; one plain statement at a time; checked first in a
transaction that is always rolled back; run for real only if the checked facts are still true; recorded in the audit log
(as JSON) BEFORE the commit, so nothing changes without a record.
"""
import re

import psycopg
import sqlparse
from django.conf import settings
from psycopg import sql

from audit.models import record

from . import pg, writes
from .pg import PgError
from .writes import SAMPLE_ROWS, _IDENT, _in_table_order, _resolve_table, _side_effects, prepare_statement

CONFIRM_WORDS = {"delete": writes.CONFIRM_WORD, "update": None, "insert": None, "truncate": "TRUNCATE", "drop": "DROP"}
STRUCTURE_KINDS = ("truncate", "drop")     # these need the server's second switch, "Also allow TRUNCATE and DROP"
_TABLE_REF = rf"{_IDENT}(?:\s*\.\s*{_IDENT})?"


def classify(text):
    """Which changing statement this is: insert, update, delete, truncate or drop. Anything else is not one of them."""
    bare = sqlparse.format(text or "", strip_comments=True).strip()
    kind = (bare.split(None, 1)[0].upper().rstrip(";").lower() if bare else "")
    if kind not in CONFIRM_WORDS:
        raise PgError("Only INSERT, UPDATE, DELETE, TRUNCATE and DROP can change data here; everything else runs read-only.")
    return kind


def _normalise(ref):
    return re.sub(r"\s*\.\s*", ".", ref)


def _run(conn, statement):
    """Run a statement; a database error becomes a plain message with its DETAIL and HINT (they say what depends on what)."""
    try:
        return conn.execute(statement)
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
        raise PgError(f"PgDeck could not save the audit record, so nothing was {what}.")
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
    wrapped = (f"WITH pgdeck_updated AS ({parsed['statement']} RETURNING {parsed['target']}.*) "
               "SELECT (SELECT count(*) FROM pgdeck_updated), "
               "(SELECT coalesce(jsonb_agg(to_jsonb(s)), '[]'::jsonb) "
               f"FROM (SELECT * FROM pgdeck_updated LIMIT {SAMPLE_ROWS}) s)")
    count, sample = _run(conn, wrapped).fetchone()
    return count, sample


def _preview_update(server, database, text):
    parsed = parse_update(text)
    with pg.connect_rw(server, database or None) as conn:
        schema, table = _resolve_table(conn, parsed["table_ref"])
        count, sample = _update_and_count(conn, parsed)
        effects = _side_effects(conn, schema, table, own="updated")
        columns, sample = _in_table_order(conn, schema, table, sample) if sample else ([], sample)
        conn.rollback()
    plural = "record" if count == 1 else "records"
    notes = []
    if not parsed["has_where"]:
        notes.append({"text": "This statement has no WHERE clause: it changes EVERY row in the table.", "bad": True})
    notes += _effect_notes(effects)
    if count > len(sample):
        notes.append({"text": f"Showing the first {len(sample)} of {count:,} rows, as they will look after the update.", "bad": False})
    return {
        "kind": "update", "statement": parsed["statement"], "schema": schema, "table": table, "count": count,
        "has_where": parsed["has_where"], "side_effects": effects, "columns": columns, "sample": sample, "objects": [],
        "title": f"UPDATE {count:,} {plural.upper()}?" if count else "UPDATE — NOTHING TO CHANGE",
        "headline": f"{count:,}",
        "headline_label": f"will be changed in {schema}.{table}." if count else "No rows match, so there is nothing to update.",
        "warning": "There is no rollback. The old values are not saved: the audit log keeps the statement and the number of "
                   "rows, but it cannot undo an UPDATE.",
        "notes": notes, "confirm_word": None, "button": f"Update {count:,} {plural}" if count else None,
        "fingerprint": {"count": count, "side_effects": effects},
    }


def _execute_update(server, database, text, fingerprint, user):
    parsed = parse_update(text)
    with pg.connect_rw(server, database or None) as conn:
        schema, table = _resolve_table(conn, parsed["table_ref"])
        count, _ = _update_and_count(conn, parsed)
        effects = _side_effects(conn, schema, table, own="updated")
        if not count:
            raise PgError("No rows match, so there is nothing to update.")
        if count != fingerprint.get("count") or effects != fingerprint.get("side_effects"):
            raise _changed_since(f"{fingerprint.get('count')} rows were expected and {count} match now.")
        payload = {"kind": "update", "database": _current_database(conn), "schema": schema, "table": table,
                   "statement": parsed["statement"], "count": count, "no_where": not parsed["has_where"], "side_effects": effects}
        entry = _commit_after_recording(conn, server, user, "update", parsed["statement"], count, payload, "updated")
    return {"count": count, "schema": schema, "table": table, "entry_id": entry.pk, "side_effects": effects,
            "message": f"Updated {count:,} row{'s' if count != 1 else ''} in {schema}.{table}. "
                       f"Recorded in the audit log as entry #{entry.pk}."}


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
        raise PgError("DROP DATABASE, ROLE and TABLESPACE are not available here; run those in psql.")
    match = _DROP.match(cleaned)
    if not match:
        raise PgError("PgDeck can drop tables, views, materialized views, indexes, sequences, schemas, functions, procedures, "
                      "types, domains, triggers, extensions and publications.")
    object_type = re.sub(r"\s+", " ", match["type"]).upper()
    rest = re.sub(r"^\s*IF\s+EXISTS\s+", "", match["rest"], flags=re.I)
    cascade = bool(re.search(r"\s+CASCADE\s*$", rest, re.I))
    rest = re.sub(r"\s+(CASCADE|RESTRICT)\s*$", "", rest, flags=re.I)
    names = []
    if object_type in ("TABLE", "MATERIALIZED VIEW"):            # for these PgDeck shows how many rows go with them
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
            "warning": "There is no rollback. Once you confirm, the rows are deleted for good. PgDeck saves a copy of every deleted "
                       "row in the audit log (as JSON), where a staff user can re-insert them, but nothing else is undone.",
            "confirm_word": CONFIRM_WORDS["delete"] if count else None,
            "button": f"Delete {count:,} {plural}" if count else None,
            "fingerprint": {"count": count, "side_effects": data["side_effects"]}}


def preview(server, database, text):
    """What a statement would do, without doing it. Returns everything the warning popup shows."""
    kind = classify(text)
    if kind == "insert":
        raise PgError("INSERT runs straight away, so there is nothing to preview.")
    return {"delete": _preview_delete, "update": _preview_update, "truncate": _preview_truncate, "drop": _preview_drop}[kind](
        server, database, text)


def execute(server, database, text, fingerprint, confirm, user):
    """Run a changing statement for real. `fingerprint` is what preview() reported; `confirm` is the word that was typed."""
    kind = classify(text)
    fingerprint = fingerprint if isinstance(fingerprint, dict) else {}
    if kind == "insert":
        result = _execute_insert(server, database, text, user)
    elif kind == "update":
        result = _execute_update(server, database, text, fingerprint, user)
    elif kind == "truncate":
        result = _execute_truncate(server, database, text, fingerprint, confirm, user)
    elif kind == "drop":
        result = _execute_drop(server, database, text, fingerprint, confirm, user)
    else:
        count = fingerprint.get("count")
        if not isinstance(count, int):
            raise PgError("That request was not understood.")
        done = writes.execute_delete(server, database, text, count, fingerprint.get("side_effects") or [], confirm, user)
        result = {**done, "message": f"Deleted {done['count']:,} row{'s' if done['count'] != 1 else ''} from {done['schema']}.{done['table']}. "
                                     f"Saved in the audit log as entry #{done['entry_id']}, where it can be re-inserted."}
    return {"kind": kind, **result}
