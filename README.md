# PgDeck

A small, safe web admin for PostgreSQL. Browse schemas, tables and rows, run read-only SQL, and keep an audit
log of everything users do. Built with Django; one command to run on a server or a laptop.

**Status: Phase 1 (MVP).** Read-only by design: every query runs in a `READ ONLY` transaction enforced by
PostgreSQL itself, with a statement timeout and a row cap. Write mode, scheduled backups and monitoring are
planned next (see the roadmap).

## Features (Phase 1)
- Sign in with local accounts; staff users manage servers, everyone signed in can browse.
- Save several PostgreSQL servers. Passwords are encrypted at rest and never shown again.
- Browse schemas, tables and views with column types, nullability and paged rows.
- SQL runner: read-only, 30 s timeout, 1000-row cap.
- Audit log of every SQL run and every server change (staff only).

## Quick start with Docker (server or local)
```bash
cp .env.example .env        # then set PGDECK_DB_PASSWORD and PGDECK_SECRET_KEY
docker compose up -d --build
docker compose exec web python manage.py createsuperuser
```
Open http://localhost:8000. The port is bound to localhost; put an https reverse proxy (Caddy, nginx) in front
before exposing it, then set `PGDECK_ALLOWED_HOSTS`, `PGDECK_CSRF_TRUSTED_ORIGINS` and `PGDECK_SECURE_COOKIES=1`.

## Quick start without Docker (Windows, macOS, Linux)
Requires Python 3.12+ and a PostgreSQL database for PgDeck's own data.
```bash
python -m venv .venv
.venv/Scripts/activate       # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env         # edit the PGDECK_DB_* values; set PGDECK_DEBUG=1 for local use
python manage.py migrate
python manage.py createsuperuser
waitress-serve --listen=127.0.0.1:8000 pgdeck.wsgi:application
```
Create the settings database first, for example `CREATE ROLE pgdeck LOGIN PASSWORD '…'; CREATE DATABASE pgdeck OWNER pgdeck;`

## Connecting to the databases you manage
Use **Add server** and give a PostgreSQL role with read access (for example `GRANT pg_read_all_data TO pgdeck_reader;`).
PgDeck never needs write access. Use `sslmode=require` or stronger for remote servers.

## Security notes
- Never expose PgDeck on the public internet without https and access restrictions (VPN or IP allow-list).
- `PGDECK_SECRET_KEY` signs sessions and, unless you set `PGDECK_ENCRYPTION_KEY`, derives the key that encrypts saved
  passwords. Changing it makes saved passwords unreadable; edit each server and re-enter its password.
- Read-only is enforced by PostgreSQL (`BEGIN READ ONLY`), not by filtering SQL text.

## Tests
PgDeck uses PostgreSQL for tests too (no SQLite). The role in your `.env` needs `CREATEDB`.
```bash
python manage.py test
```

## Roadmap
1. MVP (this release): login, servers, browse, read-only SQL, audit log.
2. Safety: opt-in write mode, confirmations for destructive SQL, per-server permissions, query history.
3. Backups: scheduled `pg_dump`, restore, retention, download.
4. Monitoring: sessions, locks, slow queries, size trends, alerts.
5. Open-source release: docs, screenshots, CI, one-line installer, Windows service.

## License
MIT — see [LICENSE](LICENSE).
