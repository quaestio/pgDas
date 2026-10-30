# PgDeck

A small, safe web admin for PostgreSQL. Browse servers, databases, schemas, tables, views, functions and more,
run read-only SQL, and keep an audit log of everything users do. Built with Django; nothing to install but PgDeck
itself — it keeps its own data in a single SQLite file.

**Status: in development.** Read-only by design: every query runs in a `READ ONLY` transaction enforced by
PostgreSQL itself, with a statement timeout and a row cap. Write mode, scheduled backups and monitoring are
planned next (see the roadmap).

## Features
- One Object Browser in the sidebar: Servers → Databases → Schemas → Tables, Views, Materialized Views, Functions,
  Procedures, Sequences, Types / Domains. Tables also show Columns, Constraints, Indexes and Triggers, with the
  `CREATE` SQL of each object.
- Sign in with local accounts; staff users manage servers and see the audit log, everyone signed in can browse.
- Save several PostgreSQL servers. Passwords are encrypted at rest and never shown again.
- SQL runner: read-only, 30 s timeout, 1000-row cap.
- Audit log of every SQL run and every server change (staff only).
- Resizable, collapsible sidebar, themed scrollbars and optional interface sounds (mute button in the header).

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
2. Safety: opt-in write mode, confirmations for destructive SQL, per-server permissions, query history.
3. Backups: scheduled `pg_dump`, restore, retention, download.
4. Monitoring: sessions, locks, slow queries, size trends, alerts.
5. Open-source release: docs, screenshots, CI, one-line installer, Windows service.

## License
MIT — see [LICENSE](LICENSE).
