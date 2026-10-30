"""Databases, users (roles) and grants: CREATE / ALTER / DROP DATABASE, CREATE / ALTER / DROP ROLE (USER, GROUP), GRANT, REVOKE.

They need the server's third switch ("Also allow databases, users and grants") on top of the other two. The popup flow is the
same as for the other statements (changes.preview / changes.execute send these statements here):

  statement                      how it is checked                              confirmation
  CREATE/ALTER ROLE, GRANT ...   run in a transaction that is rolled back,      a button; CREATE/ALTER ROLE with SUPERUSER,
                                 and the catalogs before and after compared     CREATEROLE, REPLICATION or BYPASSRLS
                                                                                asks for the word CREATE / ALTER
  ALTER DATABASE                 the same                                       a button; RENAME / OWNER TO asks for ALTER
  DROP ROLE                      the same                                       type DROP
  CREATE DATABASE, DROP DATABASE PostgreSQL cannot run these in a transaction,  CREATE: a button; DROP: type DROP
                                 so the popup describes them from the catalogs
                                 and they cannot be rolled back

Passwords are never kept: the audit log, the popup and the history show PASSWORD '********'.
"""
import re

import psycopg
import sqlparse

from audit.models import record, redact_secrets

from . import changes, pg
from .pg import PgError
from .writes import prepare_statement

_NAME = r'(?:"(?:[^"]|"")+"|[A-Za-z_\u0080-￿][\w$\u0080-￿]*)'
_START = re.compile(r"^(?:(?P<verb>CREATE|ALTER|DROP)\s+(?P<type>DATABASE|ROLE|USER|GROUP)\b(?!\s+MAPPING)|(?P<grant>GRANT|REVOKE)\b)", re.I)
_TARGET = re.compile(rf"^(?:IF\s+(?:NOT\s+)?EXISTS\s+)?(?P<name>{_NAME})", re.I)
_ESCALATION = re.compile(r"\b(SUPERUSER|CREATEROLE|REPLICATION|BYPASSRLS)\b", re.I)
_TABLESPACE_MOVE = re.compile(r"\bSET\s+TABLESPACE\b", re.I)
DROP_WORD = "DROP"


def _bare(text):
    return sqlparse.format(text or "", strip_comments=True).strip()


def is_admin_statement(text):
    """True for the statements in this module; changes.preview / execute hand those over."""
    return bool(_START.match(_bare(text)))


def _unquote(name):
    return name[1:-1].replace('""', '"') if name.startswith('"') else name.lower()


def parse(text):
    match = _START.match(_bare(text))
    if not match:
        raise PgError("This is not a database, user or grant statement.")
    verb = (match["verb"] or match["grant"]).upper()
    cleaned, _ = prepare_statement(text, verb, allow_returning=True)
    result = {"verb": verb, "statement": cleaned, "shown": redact_secrets(cleaned), "object_type": None, "name": None,
              "transactional": True, "if_exists": False, "word": None, "risky_words": []}
    plain = changes._QUOTED.sub(" ", cleaned)
    if verb in ("GRANT", "REVOKE"):
        return result
    kind = match["type"].upper()
    result["object_type"] = "DATABASE" if kind == "DATABASE" else "ROLE"
    target = _TARGET.match(cleaned[match.end():].strip())
    result["name"] = _unquote(target["name"]) if target else None
    result["if_exists"] = bool(re.match(r"\s*IF\s+(NOT\s+)?EXISTS\b", cleaned[match.end():], re.I))
    if verb in ("CREATE", "DROP") and result["object_type"] == "DATABASE":
        result["transactional"] = False
        if not result["name"]:
            raise PgError(f"Write it as {verb} DATABASE name.")
    if verb == "ALTER" and result["object_type"] == "DATABASE" and _TABLESPACE_MOVE.search(plain):
        raise PgError("Moving a database to another tablespace cannot run inside PgDeck's safety transaction; run it in psql.")
    risky = set()
    if verb in ("CREATE", "ALTER") and result["object_type"] == "ROLE":
        risky |= {w.upper() for w in _ESCALATION.findall(plain)}
    if verb == "ALTER":
        risky |= {re.sub(r"\s+", " ", w).upper() for w in changes._RISKY_ALTER.findall(plain)}
    result["risky_words"] = sorted(risky)
    if verb == "DROP":
        result["word"] = DROP_WORD
    elif risky:
        result["word"] = changes.CONFIRM_WORDS["alter"] if verb == "ALTER" else "CREATE"
    return result


# ------------------------------------------------------------------------------------------- what the catalogs say
_SNAPSHOT = """
SELECT 'role', rolname, concat_ws(',', rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin, rolreplication,
                                  rolbypassrls, rolconnlimit, coalesce(rolvaliduntil::text, ''))
FROM pg_roles WHERE rolname !~ '^pg_'
UNION ALL
SELECT 'membership', pg_get_userbyid(m.roleid) || ' -> ' || pg_get_userbyid(m.member),
       (to_jsonb(m) - 'oid' - 'roleid' - 'member' - 'grantor' - 'umid')::text
FROM pg_auth_members m
UNION ALL
SELECT 'database', datname, concat_ws(',', pg_get_userbyid(datdba), datconnlimit, datallowconn, datistemplate) FROM pg_database
UNION ALL
SELECT 'privileges', 'database ' || datname, array_to_string(datacl, E'\\x1f') FROM pg_database WHERE datacl IS NOT NULL
UNION ALL
SELECT 'privileges', 'schema ' || nspname, array_to_string(nspacl, E'\\x1f') FROM pg_namespace
WHERE nspacl IS NOT NULL AND nspname !~ '^pg_' AND nspname <> 'information_schema'
UNION ALL
SELECT 'privileges', CASE c.relkind WHEN 'S' THEN 'sequence ' WHEN 'v' THEN 'view ' WHEN 'm' THEN 'materialized view ' ELSE 'table ' END
                     || n.nspname || '.' || c.relname, array_to_string(c.relacl, E'\\x1f')
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relacl IS NOT NULL AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f') AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
UNION ALL
SELECT 'privileges', 'function ' || n.nspname || '.' || p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')',
       array_to_string(p.proacl, E'\\x1f')
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE p.proacl IS NOT NULL AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
"""


def _snapshot(conn):
    return {(kind, name): fingerprint or "" for kind, name, fingerprint in changes._run(conn, _SNAPSHOT).fetchall()}


def _items(text):
    return {item for item in (text or "").split("\x1f") if item}


def _is_owner_default(item):
    grantee, _, rest = item.partition("=")
    return grantee == rest.rpartition("/")[2]


def _diff(before, after):
    found = []
    for key in sorted(set(before) | set(after)):
        kind, name = key
        if kind == "privileges":
            old, new = _items(before.get(key)), _items(after.get(key))
            if not old:
                new = {item for item in new if not _is_owner_default(item)}     # a first GRANT also writes out the owner's own rights
            if old == new:
                continue
            added, removed = sorted(new - old), sorted(old - new)
            detail = "; ".join([f"+ {item}" for item in added] + [f"− {item}" for item in removed])
            found.append({"action": "alter", "kind": "privileges on", "name": name, "detail": detail})
        elif key not in before:
            found.append({"action": "create", "kind": kind, "name": name, "detail": ""})
        elif key not in after:
            found.append({"action": "drop", "kind": kind, "name": name, "detail": ""})
        elif before[key] != after[key]:
            found.append({"action": "alter", "kind": kind, "name": name, "detail": "settings changed"})
    return found


def _lines(found):
    marks = {"create": "+ create", "alter": "~ change", "drop": "− drop"}
    return [{"name": c["name"], "text": f"{marks[c['action']]} {c['kind']} {c['name']}" + (f" — {c['detail']}" if c["detail"] else ""),
             "rows": None, "cascade": c["action"] == "drop"} for c in found]


# ------------------------------------------------------------------------------------------------------- the popup
def _popup(parsed, found, notes, title, headline, label, button, word, warning, fingerprint, objects=None):
    return {"kind": parsed["verb"].lower(), "statement": parsed["shown"], "count": len(found), "objects": objects if objects is not None else _lines(found),
            "notes": notes, "title": title, "headline": headline, "headline_label": label, "warning": warning,
            "confirm_word": word, "button": button, "side_effects": [], "columns": [], "sample": [], "has_where": True,
            "risky": bool(word), "fingerprint": fingerprint}


_WARNINGS = {
    "CREATE": "This adds new {what}. It cannot be rolled back, but it can be removed again with DROP.",
    "ALTER": "This changes {what}. There is no rollback; it can usually be changed back with another ALTER.",
    "GRANT": "GRANT changes who may do what. There is no rollback, but REVOKE takes the rights away again.",
    "REVOKE": "REVOKE takes rights away, which can lock people or applications out. There is no rollback; GRANT gives them back.",
}


def _notes(parsed):
    notes = []
    if parsed["risky_words"]:
        notes.append({"text": "Needs extra care because of: " + ", ".join(parsed["risky_words"]) + ".", "bad": True})
    if re.search(r"\bPASSWORD\b", changes._QUOTED.sub(" ", parsed["statement"]), re.I):
        notes.append({"text": "A password is set. PgDeck does not keep it anywhere: the audit log shows PASSWORD '********'.", "bad": False})
    return notes


def _database_facts(conn, name):
    row = conn.execute("""SELECT pg_database_size(d.datname),
                                 (SELECT count(*) FROM pg_stat_activity a WHERE a.datname = d.datname AND a.pid <> pg_backend_pid())
                          FROM pg_database d WHERE d.datname = %s""", (name,)).fetchone()
    return None if row is None else {"size": row[0], "connections": row[1]}


def _preview_database(server, parsed):
    name, verb = parsed["name"], parsed["verb"]
    with pg.connect_maintenance(server, avoid=name) as conn:
        facts = _database_facts(conn, name)
    if verb == "CREATE":
        if facts:
            raise PgError(f'Database "{name}" already exists.')
        found = [{"action": "create", "kind": "database", "name": name, "detail": ""}]
        notes = _notes(parsed) + [{"text": "PostgreSQL cannot create a database inside a transaction, so this one cannot be tried out "
                                           "first: the owner, template and settings are checked when it runs.", "bad": False}]
        return _popup(parsed, found, notes, "CREATE DATABASE?", "DATABASE", f"{name} will be created.", "Create database", None,
                      _WARNINGS["CREATE"].format(what="database"), {"exists": False})
    if not facts:
        if not parsed["if_exists"]:
            raise PgError(f'Database "{name}" does not exist.')
        return _popup(parsed, [], [], "DROP DATABASE — NOTHING TO DROP", "DATABASE", "It does not exist, so there is nothing to drop.",
                      None, None, "", {"exists": False})
    found = [{"action": "drop", "kind": "database", "name": name, "detail": ""}]
    notes = [{"text": f"Every table and all data in {name} is lost ({pg.size_text(facts['size'])}).", "bad": True}]
    if facts["connections"]:
        notes.append({"text": f"{facts['connections']} connection{'s are' if facts['connections'] != 1 else ' is'} open to it. DROP fails "
                              "while others are connected, unless the statement uses WITH (FORCE), which disconnects them.", "bad": True})
    return _popup(parsed, found, notes, "DROP DATABASE?", pg.size_text(facts["size"]), f"of data in {name} will be permanently deleted.",
                  "Drop database", DROP_WORD, "There is no rollback and the audit log does not keep the data. Take a backup first if you may need it.",
                  {"exists": True})


def preview(server, database, text):
    parsed = parse(text)
    if not parsed["transactional"]:
        return _preview_database(server, parsed)
    with pg.connect_rw(server, database or None) as conn:
        before = _snapshot(conn)
        changes._run(conn, parsed["statement"])
        found = _diff(before, _snapshot(conn))
        conn.rollback()
    verb, what = parsed["verb"], (parsed["object_type"] or "privileges").lower()
    notes = _notes(parsed)
    if not found:
        return _popup(parsed, found, notes, f"{verb} — NOTHING TO DO", what.upper(), "Nothing would change.", None, None, "", {"changes": []})
    names = [c["name"] for c in found][:3]
    label = ", ".join(names) + (f" and {len(found) - 3} more" if len(found) > 3 else "") + (
        " will be created." if verb == "CREATE" else " will be removed." if verb == "DROP" else " will be changed.")
    if verb == "DROP":
        notes.append({"text": "Dropping a role also removes its memberships and the rights granted to it.", "bad": True})
    title = f"{verb} {what.upper()}?" if verb in ("CREATE", "ALTER", "DROP") else f"{verb}?"
    button = {"GRANT": "Grant", "REVOKE": "Revoke"}.get(verb, f"{verb.capitalize()} {what}")
    return _popup(parsed, found, notes, title, what.upper() if what != "privileges" else verb, label, button, parsed["word"],
                  _WARNINGS.get(verb, "There is no rollback.") if verb != "DROP"
                  else "There is no rollback. Objects the role owned must be dealt with first; PostgreSQL refuses otherwise.",
                  {"changes": found})


# --------------------------------------------------------------------------------------------------------- running
def _check_word(parsed, confirm):
    if parsed["word"] and confirm != parsed["word"]:
        raise PgError(f"Type {parsed['word']} in capital letters to confirm.")


def _payload(conn_database, parsed, found):
    return {"kind": parsed["verb"].lower(), "database": conn_database, "object_type": parsed["object_type"] or "PRIVILEGES",
            "statement": parsed["shown"], "risky": bool(parsed["word"]), "changes": found, "before": []}


def _execute_database(server, parsed, fingerprint, confirm, user):
    name, verb = parsed["name"], parsed["verb"]
    with pg.connect_maintenance(server, avoid=name, writable=True) as conn:
        facts = _database_facts(conn, name)
        if bool(facts) != fingerprint.get("exists"):
            raise changes._changed_since("the database is not in the state you were shown.")
        if verb == "DROP" and not facts:
            raise PgError("There is nothing to drop.")
        _check_word(parsed, confirm)
        found = [{"action": "create" if verb == "CREATE" else "drop", "kind": "database", "name": name, "detail": ""}]
        payload = _payload(None, parsed, found)
        if verb == "DROP":
            payload["size"], payload["connections"] = facts["size"], facts["connections"]
        try:    # these cannot be rolled back, so the record comes first and is corrected if the statement fails
            entry = record(user, verb.lower(), server, parsed["shown"], ok=True, rows=1, payload=payload)
        except Exception:
            raise PgError("PgDeck could not save the audit record, so nothing was changed.")
        try:
            changes._run(conn, parsed["statement"])
        except PgError as error:
            entry.ok = False
            entry.detail = f"{entry.detail}\n(failed: {str(error)[:200]})"
            entry.save(update_fields=["ok", "detail"])
            raise
    return {"count": 1, "entry_id": entry.pk, "changes": found,
            "message": f"Database {name} {'created' if verb == 'CREATE' else 'dropped'}. Recorded in the audit log as entry #{entry.pk}."}


def execute(server, database, text, fingerprint, confirm, user):
    parsed = parse(text)
    fingerprint = fingerprint if isinstance(fingerprint, dict) else {}
    if not parsed["transactional"]:
        return _execute_database(server, parsed, fingerprint, confirm, user)
    with pg.connect_rw(server, database or None) as conn:
        before = _snapshot(conn)
        changes._run(conn, parsed["statement"])
        found = _diff(before, _snapshot(conn))
        if not found:
            raise PgError("Nothing would change, so there is nothing to run.")
        if found != fingerprint.get("changes"):
            raise changes._changed_since("what this statement would change is not what you were shown.")
        _check_word(parsed, confirm)
        entry = changes._commit_after_recording(conn, server, user, parsed["verb"].lower(), parsed["shown"], len(found),
                                                _payload(changes._current_database(conn), parsed, found), "changed")
    verb = parsed["verb"].lower()
    done = {"create": "created", "drop": "removed"}.get(verb, "changed")
    return {"count": len(found), "entry_id": entry.pk, "changes": found,
            "message": f"{verb.capitalize()} done: {len(found)} item{'s' if len(found) != 1 else ''} {done}. "
                       f"Recorded in the audit log as entry #{entry.pk}."}
