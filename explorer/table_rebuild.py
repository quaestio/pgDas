"""Changing the order of a table's columns. PostgreSQL cannot do this in place, so the table is rebuilt:

    ALTER TABLE schema.table REORDER COLUMNS ("a", "b", "c")      (PGDAS's own statement, not PostgreSQL's)

In ONE transaction: copy the rows aside, drop the table, create it again with the columns in the new order, put the rows
back, then recreate its indexes, constraints, triggers, comment, owner and privileges, and give identity / serial counters
their old values. Before anything is committed the result is compared with the database as it was: same rows, same
indexes, constraints and triggers, same sequences, same columns in the requested order. Any difference rolls everything back.

Tables that other objects depend on (foreign keys from other tables, views, publications, row-level security, partitions,
inheritance, column privileges, extended statistics) are refused with the reason, because rebuilding them would break those
objects. The preview runs the whole rebuild and rolls it back; the person confirms by typing the table name.
"""
import re

from psycopg import sql

from . import changes, pg
from .pg import PgError
from .writes import prepare_statement

PROMPT = "Type the table name to confirm"
_NAME = r'(?:"(?:[^"]|"")+"|[A-Za-z_][\w$]*)'
_REORDER = re.compile(rf"^ALTER\s+TABLE\s+(?P<schema>{_NAME})\s*\.\s*(?P<table>{_NAME})\s+REORDER\s+COLUMNS\s*\((?P<list>.*)\)$", re.I | re.S)
_ITEM = re.compile(rf"\s*({_NAME})\s*(,|$)")


def _unquote(name):
    return name[1:-1].replace('""', '"') if name.startswith('"') else name.lower()


def _q(name):
    return '"' + name.replace('"', '""') + '"'


def is_reorder_statement(text):
    try:
        cleaned, _ = prepare_statement(text, "ALTER", allow_returning=True)
    except PgError:
        return False
    return bool(_REORDER.match(cleaned))


def parse(text):
    cleaned, _ = prepare_statement(text, "ALTER", allow_returning=True)
    match = _REORDER.match(cleaned)
    if not match:
        raise PgError('Write it as ALTER TABLE "schema"."table" REORDER COLUMNS ("a", "b", ...).')
    names, position, body = [], 0, match["list"]
    while position < len(body.rstrip()):
        item = _ITEM.match(body, position)
        if not item:
            raise PgError("The column list is not understood.")
        names.append(_unquote(item.group(1)))
        position = item.end()
    if len(set(names)) != len(names):
        raise PgError("A column is listed twice.")
    return {"schema": _unquote(match["schema"]), "table": _unquote(match["table"]), "order": names,
            "statement": f'ALTER TABLE {_q(_unquote(match["schema"]))}.{_q(_unquote(match["table"]))} REORDER COLUMNS ('
                         + ", ".join(_q(n) for n in names) + ")"}


def _facts(conn, schema, table):
    """Everything needed to rebuild the table, or a PgError that says why it cannot be rebuilt."""
    ref = f"{_q(schema)}.{_q(table)}"
    row = conn.execute(
        """SELECT c.oid, c.relkind::text, c.relispartition, c.relpersistence::text, c.reltablespace <> 0, c.relrowsecurity, c.relreplident::text,
                  pg_get_userbyid(c.relowner), c.reloptions, c.relacl IS NOT NULL, obj_description(c.oid, 'pg_class')
           FROM pg_class c WHERE c.oid = to_regclass(%s)""", (ref,)).fetchone()
    if row is None:
        raise PgError(f"Table {schema}.{table} was not found.")
    oid, relkind, is_partition, persistence, own_space, rls, replident, owner, options, has_acl, comment = row
    if relkind != "r" or is_partition:
        raise PgError("Only an ordinary table can be rebuilt (not a partitioned table, a partition, a view or a foreign table).")
    if conn.execute("SELECT 1 FROM pg_inherits WHERE inhrelid = %s OR inhparent = %s", (oid, oid)).fetchone():
        raise PgError("This table takes part in inheritance, which a rebuild would break.")
    problems = []
    if rls or conn.execute("SELECT 1 FROM pg_policy WHERE polrelid = %s", (oid,)).fetchone():
        problems.append("it has row-level security")
    if own_space:
        problems.append("it lives in its own tablespace")
    if replident != "d":
        problems.append("it has a custom replica identity")
    if conn.execute("SELECT 1 FROM pg_publication_rel WHERE prrelid = %s", (oid,)).fetchone():
        problems.append("it is part of a publication")
    if conn.execute("SELECT 1 FROM pg_statistic_ext WHERE stxrelid = %s", (oid,)).fetchone():
        problems.append("it has extended statistics")
    if conn.execute("SELECT 1 FROM pg_attribute WHERE attrelid = %s AND attacl IS NOT NULL", (oid,)).fetchone():
        problems.append("some columns have their own privileges")
    referencing = [f"{t} ({k})" for t, k in conn.execute(
        "SELECT conrelid::regclass::text, conname FROM pg_constraint WHERE confrelid = %s AND conrelid <> %s", (oid, oid)).fetchall()]
    if referencing:
        problems.append("other tables point to it with foreign keys: " + ", ".join(referencing))
    views = [v for (v,) in conn.execute(
        """SELECT DISTINCT v.oid::regclass::text FROM pg_depend d JOIN pg_rewrite r ON r.oid = d.objid JOIN pg_class v ON v.oid = r.ev_class
           WHERE d.classid = 'pg_rewrite'::regclass AND d.refobjid = %s AND v.oid <> %s""", (oid, oid)).fetchall()]
    if views:
        problems.append("these views use it: " + ", ".join(views))
    if problems:
        raise PgError("This table cannot be rebuilt safely because " + "; ".join(problems) + ". Drop or change those first, or run the change in psql.")

    columns = []
    for number, name, type_text, notnull, identity, generated, default, collation, comment_text in conn.execute(
            """SELECT a.attnum, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, a.attidentity::text, a.attgenerated::text,
                      pg_get_expr(d.adbin, d.adrelid),
                      CASE WHEN a.attcollation <> 0 AND a.attcollation <> t.typcollation
                           THEN (SELECT quote_ident(cn.nspname) || '.' || quote_ident(co.collname) FROM pg_collation co JOIN pg_namespace cn ON cn.oid = co.collnamespace
                                 WHERE co.oid = a.attcollation) END,
                      col_description(a.attrelid, a.attnum)
               FROM pg_attribute a JOIN pg_type t ON t.oid = a.atttypid LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
               WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum""", (oid,)).fetchall():
        column = {"name": name, "type": type_text, "notnull": notnull, "identity": identity, "generated": generated, "default": default,
                  "collation": collation, "comment": comment_text, "sequence": None}
        if identity:
            seq = conn.execute("SELECT pg_get_serial_sequence(%s, %s)", (ref, name)).fetchone()[0]
            start, step, low, high, cache, cycle = conn.execute(
                "SELECT seqstart, seqincrement, seqmin, seqmax, seqcache, seqcycle FROM pg_sequence WHERE seqrelid = %s::regclass", (seq,)).fetchone()
            last, called = conn.execute(f"SELECT last_value, is_called FROM {seq}").fetchone()
            column["sequence"] = {"name": seq, "options": f"START WITH {start} INCREMENT BY {step} MINVALUE {low} MAXVALUE {high} CACHE {cache}"
                                                           + (" CYCLE" if cycle else " NO CYCLE"), "last": last, "called": called}
        columns.append(column)
    owned = {}
    for seq, column_name in conn.execute(
            """SELECT s.oid::regclass::text, a.attname FROM pg_depend d JOIN pg_class s ON s.oid = d.objid AND s.relkind = 'S'
               JOIN pg_attribute a ON a.attrelid = d.refobjid AND a.attnum = d.refobjsubid
               WHERE d.classid = 'pg_class'::regclass AND d.refobjid = %s AND d.deptype = 'a'""", (oid,)).fetchall():
        owned[column_name] = seq
    for column in columns:
        column["owned_sequence"] = owned.get(column["name"])
    indexes = conn.execute(
        """SELECT pg_get_indexdef(x.indexrelid) FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid
           WHERE x.indrelid = %s AND NOT EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conindid = x.indexrelid AND k.conrelid = x.indrelid AND k.contype IN ('p', 'u', 'x'))
           ORDER BY i.relname""", (oid,)).fetchall()
    constraints = conn.execute(
        """SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = %s AND contype IN ('p', 'u', 'x', 'c', 'f')
           ORDER BY CASE contype WHEN 'p' THEN 0 WHEN 'u' THEN 1 WHEN 'x' THEN 1 WHEN 'c' THEN 2 ELSE 3 END, conname""", (oid,)).fetchall()
    triggers = conn.execute("SELECT tgname, pg_get_triggerdef(oid), tgenabled::text FROM pg_trigger WHERE tgrelid = %s AND NOT tgisinternal ORDER BY tgname",
                            (oid,)).fetchall()
    privileges = conn.execute(
        """SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(a.grantee)) END, a.privilege_type, a.is_grantable
           FROM pg_class c, aclexplode(c.relacl) a WHERE c.oid = %s""", (oid,)).fetchall() if has_acl else []
    return {"oid": oid, "ref": ref, "owner": owner, "persistence": persistence, "options": options, "has_acl": has_acl, "comment": comment,
            "columns": columns, "indexes": [i for (i,) in indexes], "constraints": constraints, "triggers": triggers, "privileges": privileges}


def _column_definition(column):
    parts = [_q(column["name"]), column["type"]]
    if column["collation"]:
        parts.append("COLLATE " + column["collation"])
    if column["generated"]:
        parts.append(f"GENERATED ALWAYS AS ({column['default']}) STORED")
    elif column["identity"]:
        parts.append(f"GENERATED {'ALWAYS' if column['identity'] == 'a' else 'BY DEFAULT'} AS IDENTITY ({column['sequence']['options']})")
    elif column["default"] is not None:
        parts.append("DEFAULT " + column["default"])
    if column["notnull"] and not column["identity"]:
        parts.append("NOT NULL")
    return " ".join(parts)


def _rebuild(conn, schema, table, order):
    """Do the whole rebuild inside the open transaction and check it. The caller commits or rolls back."""
    facts = _facts(conn, schema, table)
    by_name = {c["name"]: c for c in facts["columns"]}
    if sorted(order) != sorted(by_name):
        raise PgError("The new order must list every column of the table exactly once. The table may have changed; open it again.")
    before = changes._snapshot(conn)
    rows = changes._count(conn, schema, table)
    ref, new_columns = facts["ref"], [by_name[n] for n in order]
    stored = [c for c in new_columns if not c["generated"]]
    copy_list = ", ".join(_q(c["name"]) for c in stored)
    conn_run = lambda statement: changes._run(conn, statement)

    conn_run(f"CREATE TEMP TABLE pgdas_reorder_copy ON COMMIT DROP AS SELECT {copy_list} FROM {ref}")
    for column in facts["columns"]:
        if column["owned_sequence"]:
            conn_run(f"ALTER SEQUENCE {column['owned_sequence']} OWNED BY NONE")      # the serial counter must survive the DROP
    conn_run(f"DROP TABLE {ref}")
    options = f" WITH ({', '.join(facts['options'])})" if facts["options"] else ""
    conn_run(f"CREATE {'UNLOGGED ' if facts['persistence'] == 'u' else ''}TABLE {ref} (\n    " + ",\n    ".join(_column_definition(c) for c in new_columns)
             + f"\n){options}")
    if any(c["identity"] for c in stored):
        conn_run(f"INSERT INTO {ref} ({copy_list}) OVERRIDING SYSTEM VALUE SELECT {copy_list} FROM pgdas_reorder_copy")
    else:
        conn_run(f"INSERT INTO {ref} ({copy_list}) SELECT {copy_list} FROM pgdas_reorder_copy")
    for column in facts["columns"]:
        if column["identity"]:
            seq = conn.execute("SELECT pg_get_serial_sequence(%s, %s)", (ref, column["name"])).fetchone()[0]
            conn.execute("SELECT setval(%s::regclass, %s, %s)", (seq, column["sequence"]["last"], column["sequence"]["called"]))
        if column["owned_sequence"]:
            conn_run(f"ALTER SEQUENCE {column['owned_sequence']} OWNED BY {ref}.{_q(column['name'])}")
    for definition in facts["indexes"]:
        conn_run(definition)
    for name, definition in facts["constraints"]:
        conn_run(f"ALTER TABLE {ref} ADD CONSTRAINT {_q(name)} {definition}")
    for name, definition, enabled in facts["triggers"]:
        conn_run(definition)
        if enabled != "O":
            conn_run(f"ALTER TABLE {ref} {'DISABLE' if enabled == 'D' else 'ENABLE ALWAYS' if enabled == 'A' else 'ENABLE REPLICA'} TRIGGER {_q(name)}")
    if facts["comment"]:
        conn.execute(sql.SQL("COMMENT ON TABLE {} IS {}").format(sql.SQL(ref), sql.Literal(facts["comment"])))
    for column in facts["columns"]:
        if column["comment"]:
            conn.execute(sql.SQL("COMMENT ON COLUMN {}.{} IS {}").format(sql.SQL(ref), sql.SQL(_q(column["name"])), sql.Literal(column["comment"])))
    conn_run(f"ALTER TABLE {ref} OWNER TO {_q(facts['owner'])}")
    if facts["has_acl"]:
        conn_run(f"REVOKE ALL ON TABLE {ref} FROM PUBLIC")
        conn_run(f"REVOKE ALL ON TABLE {ref} FROM {_q(facts['owner'])}")
        for who, privilege, grantable in facts["privileges"]:
            conn_run(f"GRANT {privilege} ON TABLE {ref} TO {who}" + (" WITH GRANT OPTION" if grantable else ""))

    # The safety net: nothing may differ from before except the order of this table's columns.
    after = changes._snapshot(conn)
    key = ("relation", f"{schema}.{table}")
    for other in set(before) | set(after):
        if other == key:
            old, new = before[other].split("\x1d"), after.get(other, "").split("\x1d")
            if len(new) != len(old) or [old[0], old[1], old[3], old[4]] != [new[0], new[1], new[3], new[4]] \
                    or sorted(old[2].split("\x1f")) != sorted(new[2].split("\x1f")):
                raise PgError("After the rebuild the table is not the same as before (apart from the order), so everything was undone.")
        elif before.get(other) != after.get(other):
            raise PgError(f"After the rebuild {other[0]} {other[1]} is not the same as before, so everything was undone.")
    if changes._count(conn, schema, table) != rows:
        raise PgError("After the rebuild the table has a different number of rows, so everything was undone.")
    if [c["name"] for c in pg.table_columns(conn, schema, table)] != order:
        raise PgError("The columns did not end up in the requested order, so everything was undone.")
    return {"rows": rows, "indexes": len(facts["indexes"]) + sum(1 for n, d in facts["constraints"] if d.startswith(("PRIMARY", "UNIQUE", "EXCLUDE"))),
            "constraints": len(facts["constraints"]), "triggers": len(facts["triggers"]), "before": [c["name"] for c in facts["columns"]]}


def _plural(n, word):
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def preview(server, database, text):
    parsed = parse(text)
    schema, table = parsed["schema"], parsed["table"]
    with pg.connect_rw(server, database or None) as conn:
        result = _rebuild(conn, schema, table, parsed["order"])
        conn.rollback()
    if result["before"] == parsed["order"]:
        raise PgError("That is the order the columns already have.")
    notes = [
        {"text": f"The table is locked completely (no reads, no writes) from the moment this starts until it finishes. "
                 f"{_plural(result['rows'], 'row')} are copied, so a big table takes a while.", "bad": True},
        {"text": f"It is dropped and created again with the columns in the new order. Recreated the same way: {_plural(result['indexes'], 'index')}, "
                 f"{_plural(result['constraints'], 'constraint')}, {_plural(result['triggers'], 'trigger')}, the comment, owner and privileges.", "bad": False},
        {"text": "Identity and serial counters keep their values. Everything is checked against the old table before it is committed; "
                 "if anything differs it is undone.", "bad": False},
        {"text": "Anything not listed above (for example a cached query plan or a dependency PGDAS does not know) is not carried over.", "bad": False},
    ]
    return {"kind": "alter", "statement": parsed["statement"], "count": 1,
            "objects": [{"name": f"{schema}.{table}", "text": f"~ rebuild table {schema}.{table} ({_plural(result['rows'], 'row')}) — columns: "
                         + ", ".join(parsed["order"]), "rows": result["rows"], "cascade": False}],
            "notes": notes, "title": "REBUILD TABLE?", "headline": "1", "headline_label": f"table, {schema}.{table}, will be rebuilt in a new column order.",
            "warning": "There is no rollback once you confirm, and the table stays locked until it is done. The audit log keeps the previous "
                       "CREATE statement of the table, not a copy of its data. Take a backup first if the data matters.",
            "confirm_word": table, "confirm_prompt": PROMPT, "button": "Rebuild table",
            "side_effects": [], "columns": [], "sample": [], "has_where": True, "risky": True,
            "fingerprint": {"rows": result["rows"], "before": result["before"], "order": parsed["order"]}}


def execute(server, database, text, fingerprint, confirm, user):
    parsed = parse(text)
    schema, table = parsed["schema"], parsed["table"]
    if confirm != table:
        raise PgError("Type the table name exactly to confirm (capital letters matter). Nothing was changed.")
    with pg.connect_rw(server, database or None) as conn:
        definition = pg.table_ddl(conn, schema, table)
        result = _rebuild(conn, schema, table, parsed["order"])
        if result["rows"] != fingerprint.get("rows") or result["before"] != fingerprint.get("before") or parsed["order"] != fingerprint.get("order"):
            raise changes._changed_since("the table is not what you were shown.")
        payload = {"kind": "alter", "database": changes._current_database(conn), "object_type": "TABLE", "statement": parsed["statement"],
                   "risky": True, "reorder": True, "rows": result["rows"], "before_order": result["before"], "after_order": parsed["order"],
                   "before": [{"name": f"{schema}.{table}", "sql": definition}]}
        entry = changes._commit_after_recording(conn, server, user, "alter", parsed["statement"], 1, payload, "changed")
    return {"count": 1, "entry_id": entry.pk, "changes": [],
            "message": f"Table {schema}.{table} rebuilt with its columns in the new order ({_plural(result['rows'], 'row')} kept). "
                       f"Recorded in the audit log as entry #{entry.pk}."}
