# PgDeck

A small, safe web admin for PostgreSQL. Browse servers, databases, schemas, tables, views, functions and more,
run read-only SQL, and keep an audit log of everything users do. Built with Django; nothing to install but PgDeck
itself — it keeps its own data in a single SQLite file.

**Status: in development.** Read-only by design: every query runs in a `READ ONLY` transaction enforced by
PostgreSQL itself, with a statement timeout and a row cap. Write mode, scheduled backups and monitoring are
planned next (see the roadmap).

## Features
- One Object Browser in the sidebar, laid out like pgAdmin's:
  - **Server:** Databases, Roles (login and group roles, with their memberships), Tablespaces.
  - **Database:** Catalogs (`pg_catalog`, `information_schema`), Extensions, Event Triggers, Publications, Schemas.
  - **Schema:** Tables, Views, Materialized Views, Functions, Procedures, Sequences, Types / Domains. Tables also show
    Columns, Constraints, Indexes and Triggers.
  - Every object has a Properties tab and the `CREATE` SQL for it. Role SQL never includes a password.
  - A **system** switch in the sidebar also shows built-in `pg_*` roles and objects installed by extensions.
- Sign in with local accounts; staff users manage servers and see the audit log, everyone signed in can browse.
- Save several PostgreSQL servers. Passwords are encrypted at rest and never shown again.
- SQL runner: read-only, 30 s timeout, 1000-row cap. Optional, per server: INSERT, UPDATE, DELETE, TRUNCATE, DROP, CREATE and ALTER with
  warning popups and typed confirmation (see "Changing data").
- Query history: the SQL page lists your own latest 20 statements on that server; click one to reuse it.
- Audit log of every SQL run, server change and data change (staff only), as JSON, with a Re-insert button for deletes and an Undo button for updates.
- Resizable, collapsible sidebar, themed scrollbars and optional interface sounds (mute button in the header).

## Changing data (opt-in, with popups and a safety net)
PgDeck is read-only until you switch changes on **per server**, with three separate switches in **Edit server**:

| Switch | Allows | How each one asks |
|---|---|---|
| **Allow changing data** | `INSERT`, `UPDATE`, `DELETE` | `INSERT` runs at once. `UPDATE` shows a popup with the **number of rows** it changes and a warning if it has **no WHERE**, then a button. `DELETE` shows its popup and asks you to type **`ZAP`**. |
| **Also allow CREATE, ALTER, TRUNCATE and DROP** (needs the first) | `CREATE`, `ALTER`, `TRUNCATE`, `DROP` | A warning popup. `TRUNCATE` and `DROP` need you to type **`TRUNCATE`** or **`DROP`**; `CREATE` and `ALTER` list what changes (a risky `ALTER` asks for **`ALTER`**). |
| **Also allow databases, users and grants** (needs the second) | `CREATE`/`ALTER`/`DROP` `DATABASE` and `ROLE` (`USER`, `GROUP`), `GRANT`, `REVOKE` | A popup listing what changes. Dropping asks for **`DROP`**; a role with `SUPERUSER`, `CREATEROLE`, `REPLICATION` or `BYPASSRLS` asks for **`CREATE`**/**`ALTER`**. |

All three are off by default, only staff users can use them, and turning either on or off is written to the audit log. Every
change is checked first in a transaction that is always rolled back, run for real only if the facts you were shown are
still true, and recorded in the audit log (as JSON) *before* it is committed, so nothing changes without a record. Any
other statement, and anything typed with scripting switched off, still runs read-only.

- **UPDATE:** the popup shows the count, a sample of the rows as they will look, and any other tables a trigger would
  change. The old and new values of every updated row are saved in the audit log (as JSON), with an **Undo** button. Undo
  puts back the changed columns, all or nothing, and only if no updated row was changed or deleted again since. It needs a
  primary key and at most `PGDECK_MAX_UNDO_ROWS` rows (10 000 by default); otherwise the popup says the update cannot be undone.
  Side effects of triggers are not undone.
- **INSERT:** the audit record holds the statement and the number of rows. `INSERT … RETURNING` shows what came back.
- **TRUNCATE:** the popup lists each table and its row count (with `CASCADE`, also the tables that point at it). The
  rows are **not** saved, because a table can hold millions; the audit record keeps the tables and counts.
- **DROP** (tables, views, materialized views, indexes, sequences, schemas, functions, procedures, types, domains,
  triggers, extensions, publications): the popup shows the rows that go with a dropped table and everything `CASCADE`
  would take along. The audit record keeps the `CREATE` statement of dropped tables for reference. `DROP DATABASE`,
  `ROLE` and `TABLESPACE` are not available here.
- **CREATE / ALTER:** the statement is tried in the rolled-back transaction and the popup lists exactly what would be created or changed (tables, columns, indexes, constraints, sequences...). A plain `CREATE` or `ALTER` needs just a button; an `ALTER` that drops, renames, changes a type or owner, or takes another object with it asks you to type **`ALTER`**. The audit record keeps the changes and the previous `CREATE` of an altered table or view. `CREATE DATABASE/ROLE/TABLESPACE`, temporary objects and `CONCURRENTLY` are not available.
- **Databases, users and grants:** roles and grants are tried in the rolled-back transaction and the popup lists the roles, memberships and privileges that change. ``CREATE DATABASE`` and ``DROP DATABASE`` cannot run inside a transaction, so they are described from the catalogs and cannot be rolled back (``DROP DATABASE`` shows the size and open connections). With the third switch on, the **Databases** list has a **Create database** form (name, owner, encoding) that opens the same review popup. **Passwords are never kept:** the popup, the audit log and the history show ``PASSWORD '********'``. The login saved for the server needs the matching PostgreSQL rights (``CREATEDB``, ``CREATEROLE``, grant option).
- Each takes **one plain statement at a time** (no `WITH`, no `RETURNING` on UPDATE/DELETE) and, for row changes,
  ordinary tables only.

### Deleting rows
On such a server, a `DELETE` typed into the SQL runner works like this:
1. **Check:** PgDeck runs it inside a transaction that is always rolled back, and shows a popup with the **number of
   records** that would be deleted, a sample of them, whether the statement has no `WHERE`, and any *other* tables it
   would also change (`ON DELETE CASCADE`, triggers).
2. **Warning:** the popup says there is **no rollback** and asks you to type **`ZAP`** in capital letters (`zap`, `Zap`
   or a stray space does not count). The Delete button stays disabled until the box holds exactly `ZAP`.
3. **Delete:** PgDeck runs it again and commits only if the number of rows and the side effects are still exactly what
   you confirmed; otherwise nothing is deleted and you are asked to check again.
4. **Record:** every deleted row is saved in the audit log as **JSON** (database, schema, table, the statement, the
   column names and each row, with types kept: numbers, nested JSON, arrays, NULLs). The record is saved *before* the
   delete is committed: if it cannot be saved, nothing is deleted. A failed or refused attempt is logged too.
5. **Re-insert:** in the audit log, **Re-insert N rows** (after a confirmation popup) puts the rows back, all or nothing,
   once per record. **Download JSON** gives the whole record.

Limits, on purpose: one plain `DELETE FROM table …` at a time (no `WITH`, no `RETURNING`); ordinary tables only; at most
`PGDECK_MAX_DELETE_ROWS` (default 10,000) rows per delete, because each is stored for re-insertion. Rows removed by
`ON DELETE CASCADE` or a trigger are shown in the popup but are **not** saved, so re-insert cannot bring them back.
Re-insert fails, and changes nothing, if a row with the same key now exists or a column was dropped. Give the PostgreSQL
login only the rights it needs (for example `DELETE` on the tables staff may clear): PgDeck can only do what that login may.

## Where PgDeck keeps its own data
PgDeck needs **no database server of its own**. Its users, saved servers, audit log and sessions are stored in one
SQLite file, `data/pgdeck.sqlite3` (change it with `PGDECK_SQLITE_PATH`). The file and its folder are created on
first start. **Back it up** by copying the file while PgDeck is stopped, or with `sqlite3 data/pgdeck.sqlite3 ".backup copy.sqlite3"`.
SQLite allows one writer at a time, which is plenty for a team browsing and querying; it is not meant for dozens of
people writing at the same moment.

The PostgreSQL servers you *browse* are separate: you add them in the app.

## Quick start with Docker (server or local)
```bash
cp .env.example .env        # then set PGDECK_SECRET_KEY
docker compose up -d --build
docker compose exec pgdeck python manage.py createsuperuser
```
Open http://localhost:8000. The data lives in the `pgdeck-data` volume. The port is bound to localhost; put an https
reverse proxy (Caddy, nginx) in front before exposing it, then set `PGDECK_ALLOWED_HOSTS`,
`PGDECK_CSRF_TRUSTED_ORIGINS` and `PGDECK_SECURE_COOKIES=1`.

## Quick start without Docker (Windows, macOS, Linux)
Requires Python 3.12+. No database to set up.
```bash
python -m venv .venv
.venv/Scripts/activate       # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env         # set PGDECK_DEBUG=1 for local use, or PGDECK_SECRET_KEY for a server
python manage.py migrate
python manage.py createsuperuser
waitress-serve --listen=127.0.0.1:8000 pgdeck.wsgi:application
```
Use the virtual environment's Python (the `.venv` above), not your system Python, or packages will be missing.

## Connecting to the databases you manage
Use **Add server** and give a name, host, port, a PostgreSQL role with read access (for example
`GRANT pg_read_all_data TO pgdeck_reader;`), its password and an SSL mode. There is no database to fill in:
PgDeck logs into a database the role can open (it tries `postgres`, then a database named after the role, then
`template1`) and the tree lists only the databases that role is allowed to connect to. Adding a server never
creates or changes anything on it. PgDeck never needs write access. Use `sslmode=require` or stronger for
remote servers.

## Security notes
- Never expose PgDeck on the public internet without https and access restrictions (VPN or IP allow-list).
- `PGDECK_SECRET_KEY` signs sessions and, unless you set `PGDECK_ENCRYPTION_KEY`, derives the key that encrypts saved
  passwords. Changing it makes saved passwords unreadable; edit each server and re-enter its password.
- `data/pgdeck.sqlite3` holds password hashes and encrypted server passwords: keep it out of git (it is ignored),
  out of shared folders, and readable only by the account that runs PgDeck.
- Read-only is enforced by PostgreSQL (`BEGIN READ ONLY`), not by filtering SQL text.

## Tests
PgDeck's own data uses SQLite, so most tests need nothing. The explorer tests browse a real PostgreSQL server and
create and drop a scratch database `pgdeck_test_target` on it, so they need a login that can create databases:
```bash
# in .env, or as environment variables
PGDECK_TEST_PG_HOST=localhost
PGDECK_TEST_PG_PORT=5432
PGDECK_TEST_PG_USER=postgres
PGDECK_TEST_PG_PASSWORD=...
python manage.py test
```
Without a reachable PostgreSQL those tests are skipped and say why.

## Roadmap
1. MVP: login, servers, object browser, read-only SQL, audit log.
2. Safety: opt-in write mode — **INSERT, UPDATE, DELETE, TRUNCATE, DROP, CREATE and ALTER with popups, typed confirmation, JSON audit
   and re-insert for deletes and undo for updates are done**; query history is done; next: finer per-user permissions.
3. Backups: scheduled `pg_dump`, restore, retention, download.
4. Monitoring: sessions, locks, slow queries, size trends, alerts.
5. Open-source release: docs, screenshots, CI, one-line installer, Windows service.

## License
MIT — see [LICENSE](LICENSE).
