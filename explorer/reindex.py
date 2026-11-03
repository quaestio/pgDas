"""REINDEX INDEX name / REINDEX TABLE name: rebuild one index or every index of a table (after bloat, or a corrupted index).

Plain REINDEX only: it locks writes to the table while it runs. CONCURRENTLY cannot run inside PGDAS's safety transaction, and
DATABASE, SCHEMA and SYSTEM are too wide for a button. The preview lists the indexes and their sizes; the run is recorded.
"""
import re

from . import changes, pg
from .pg import PgError
from .writes import _IDENT, prepare_statement

_REINDEX = re.compile(rf"^REINDEX\s+(?P<type>INDEX|TABLE)\s+(?P<name>{_IDENT}(?:\s*\.\s*{_IDENT})?)$", re.I)


def is_reindex_statement(text):
    return bool(re.match(r"^\s*REINDEX\b", text or "", re.I))


def parse(text):
    cleaned, _ = prepare_statement(text, "REINDEX", allow_returning=True)
    if re.search(r"\bCONCURRENTLY\b", cleaned, re.I):
        raise PgError("REINDEX CONCURRENTLY cannot run inside PGDAS's safety transaction. Use plain REINDEX, or run it in psql.")
    match = _REINDEX.match(cleaned)
    if not match:
        raise PgError("PGDAS runs REINDEX INDEX name or REINDEX TABLE name (not DATABASE, SCHEMA or SYSTEM).")
    return {"statement": cleaned, "type": match["type"].upper(), "name": re.sub(r"\s*\.\s*", ".", match["name"])}


def _targets(conn, parsed):
    """The indexes this statement rebuilds, as [(schema.name, bytes)]."""
    if parsed["type"] == "INDEX":
        rows = conn.execute("SELECT n.nspname || '.' || c.relname, pg_relation_size(c.oid) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                            "WHERE c.oid = to_regclass(%s) AND c.relkind IN ('i', 'I')", (parsed["name"],)).fetchall()
    else:
        rows = conn.execute("SELECT n.nspname || '.' || i.relname, pg_relation_size(i.oid) FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid "
                            "JOIN pg_namespace n ON n.oid = i.relnamespace WHERE x.indrelid = to_regclass(%s) ORDER BY i.relname", (parsed["name"],)).fetchall()
    if not rows:
        raise PgError(f"{parsed['name']} was not found, or it has no indexes.")
    return [(name, int(size)) for name, size in rows]


def preview(server, database, text):
    parsed = parse(text)
    with pg.connect_rw(server, database or None) as conn:
        targets = _targets(conn, parsed)
        changes._run(conn, parsed["statement"])              # try it for real, then roll back: it must work before the person confirms
        conn.rollback()
    total = sum(size for _, size in targets)
    return {"kind": "reindex", "statement": parsed["statement"], "count": len(targets),
            "objects": [{"name": n, "text": f"~ rebuild index {n} ({pg.size_text(s)})", "rows": None, "cascade": False} for n, s in targets],
            "notes": [{"text": "Writes to the table are blocked while this runs, and reads that use the index wait for it. "
                               f"{pg.size_text(total)} of indexes are rebuilt, so a big table takes a while.", "bad": True}],
            "title": "REINDEX?", "headline": f"{len(targets):,}", "headline_label": f"index{'' if len(targets) == 1 else 'es'} will be rebuilt.",
            "warning": "REINDEX rebuilds the index from the table's data. The data is not changed, and if it fails nothing changes.",
            "confirm_word": None, "button": "Reindex", "side_effects": [], "columns": [], "sample": [], "has_where": True,
            "fingerprint": {"indexes": [n for n, _ in targets]}}


def execute(server, database, text, fingerprint, confirm, user):
    parsed = parse(text)
    with pg.connect_rw(server, database or None) as conn:
        targets = _targets(conn, parsed)
        if [n for n, _ in targets] != fingerprint.get("indexes"):
            raise changes._changed_since("the indexes are not what you were shown.")
        changes._run(conn, parsed["statement"])
        payload = {"kind": "reindex", "database": changes._current_database(conn), "statement": parsed["statement"],
                   "object_type": parsed["type"], "indexes": [n for n, _ in targets], "count": len(targets)}
        entry = changes._commit_after_recording(conn, server, user, "reindex", parsed["statement"], len(targets), payload, "rebuilt")
    return {"count": len(targets), "entry_id": entry.pk,
            "message": f"Rebuilt {len(targets)} index{'' if len(targets) == 1 else 'es'}. Recorded in the audit log as entry #{entry.pk}."}
