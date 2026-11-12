"""Parse PostgreSQL SQL before checking actions. Never authorize from a regex prefix."""
import json
from pglast.parser import parse_sql_json
from django.core.exceptions import PermissionDenied
from .policy import require
from . import runtime

READ_TYPES = {"SelectStmt", "VariableShowStmt", "ExplainStmt"}
ACTIONS = {"InsertStmt": "insert", "UpdateStmt": "update", "DeleteStmt": "delete", "TruncateStmt": "truncate",
           "CreateStmt": "create", "IndexStmt": "create", "ViewStmt": "views", "CreateSeqStmt": "sequences",
           "AlterTableStmt": "alter", "AlterSeqStmt": "sequences", "RenameStmt": "alter", "DropStmt": "drop",
           "CreateSchemaStmt": "schemas", "CreateFunctionStmt": "functions", "AlterFunctionStmt": "functions",
           "CreateTrigStmt": "triggers", "CreateExtensionStmt": "extensions"}
# What a DROP removes decides which permission it needs.
DROP_ACTIONS = {"OBJECT_TABLE": "drop", "OBJECT_INDEX": "drop", "OBJECT_VIEW": "views", "OBJECT_MATVIEW": "views", "OBJECT_SEQUENCE": "sequences",
                "OBJECT_SCHEMA": "schemas", "OBJECT_FUNCTION": "functions", "OBJECT_PROCEDURE": "functions", "OBJECT_TRIGGER": "triggers",
                "OBJECT_EXTENSION": "extensions"}


def action_for(kind, value):
    if kind == "DropStmt":
        return DROP_ACTIONS.get(value.get("removeType"), "drop")
    return ACTIONS[kind]
SAFE_FUNCTIONS = {"count", "sum", "avg", "min", "max", "lower", "upper", "length", "char_length", "abs", "round", "ceil",
                  "floor", "concat", "concat_ws", "substring", "replace", "trim", "btrim", "ltrim", "rtrim", "date_part",
                  "date_trunc", "now", "current_date", "array_agg", "string_agg", "json_agg", "jsonb_agg", "to_char"}


def parsed(text):
    try:
        statements = json.loads(parse_sql_json(text))["stmts"]
    except Exception:
        raise PermissionDenied("Invalid or unsupported PostgreSQL statement.")
    if len(statements) != 1:
        raise PermissionDenied("Run exactly one statement at a time.")
    return statements[0]["stmt"]


def walk(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def authorize(actor, server, database, text, *, write=False):
    require(actor, "read", server=server, database=database)
    if not write:
        require(actor, "run_sql", server=server, database=database)           # typing one's own SQL is a permission of its own
    statement = parsed(text)
    top = next(iter(statement))
    admin = runtime.is_admin(actor)
    if write and admin:
        # Existing write parsers enforce supported single-statement forms and confirmations.
        return
    if not write and top not in READ_TYPES:
        raise PermissionDenied("Use the reviewed write workflow for changes. Transaction-control statements are not allowed.")
    if write and top not in ACTIONS:
        raise PermissionDenied("This statement is reserved for Super Users.")
    for node in walk(statement):
        for kind, value in node.items():
            if kind.endswith("Stmt") and kind[0].isupper():
                if kind in ACTIONS:
                    if not write:
                        raise PermissionDenied("Read queries cannot contain data-changing statements.")
                    require(actor, action_for(kind, value), server=server, database=database)
                elif kind not in READ_TYPES:
                    raise PermissionDenied("Unsupported nested SQL operation.")
            if not admin:
                if kind in {"IntoClause", "intoClause", "LockingClause", "lockingClause", "OnConflictClause", "onConflictClause"}:
                    raise PermissionDenied("SELECT INTO, row locks and ON CONFLICT require a Super User.")
                if kind == "FuncCall":
                    names = [part["String"]["sval"] for part in value["funcname"]]
                    if names[-1] not in SAFE_FUNCTIONS or len(names) > 2 or (len(names) == 2 and names[0] != "pg_catalog"):
                        raise PermissionDenied("This function is not available in employee SQL. Ask a Super User to run it.")
                if kind == "RenameStmt" and value.get("renameType") not in {"OBJECT_TABLE", "OBJECT_COLUMN", "OBJECT_INDEX", "OBJECT_VIEW", "OBJECT_SEQUENCE"}:
                    raise PermissionDenied("This rename operation requires a Super User.")
                if kind == "TypeCast":
                    names = [part["String"]["sval"] for part in value.get("typeName", {}).get("names", [])]
                    if not names or names[-1] not in {"text", "varchar", "bpchar", "int2", "int4", "int8", "numeric", "float4", "float8", "bool", "date", "time", "timestamp", "timestamptz", "interval", "uuid", "json", "jsonb", "bytea"} or (len(names) > 1 and names[0] != "pg_catalog"):
                        raise PermissionDenied("Employee SQL can cast only to supported built-in types.")
                if kind == "RangeVar" or (kind == "relation" and isinstance(value, dict)):
                    if value.get("catalogname") or value.get("schemaname", "").startswith("pg_") or value.get("schemaname") == "information_schema" or value.get("relname", "").startswith("pg_"):
                        raise PermissionDenied("Employee SQL cannot access server catalogs or another database.")
                if kind in {"CreateStmt", "ViewStmt", "AlterTableStmt", "IndexStmt", "CreateSeqStmt", "AlterSeqStmt", "RenameStmt", "DropStmt", "CreateSchemaStmt"}:
                    # Structure changes can invoke extension hooks, ownership changes or privileged expressions.
                    # Limit to ordinary table/index/view/sequence operations; privileges still apply below.
                    if kind == "DropStmt" and value.get("removeType") not in DROP_ACTIONS:
                        raise PermissionDenied("This kind of DROP is reserved for Super Users.")
                    if kind == "AlterTableStmt" and any(c.get("AlterTableCmd", {}).get("subtype") not in {"AT_AddColumn", "AT_DropColumn", "AT_SetNotNull", "AT_DropNotNull"} for c in value.get("cmds", [])):
                        raise PermissionDenied("This ALTER operation requires a Super User.")
                if kind == "CreateSchemaStmt" and value.get("authrole"):
                    raise PermissionDenied("CREATE SCHEMA ... AUTHORIZATION (another owner) is reserved for Super Users.")
                if kind == "DefElem" and value.get("defname") in {"security_definer", "security", "trusted", "connection", "password"}:
                    raise PermissionDenied("Privileged SQL options are not allowed.")


def authorize_effects(actor, server, database, effects):
    for effect in effects:
        for counter, action in (("inserted", "insert"), ("updated", "update"), ("deleted", "delete")):
            if effect.get(counter, 0):
                require(actor, action, server=server, database=database)
