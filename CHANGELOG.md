# Changelog

All notable changes to PGDAS, newest first. Versions follow [Semantic Versioning](https://semver.org/): a change that needs
action when you upgrade raises the first number, new features the second, fixes the third.

## 1.0.0 — 2026-11-16

The first release of PGDAS, the PostgreSQL Database Access System: a web admin for PostgreSQL built with Django. The footer of every
page now shows the version.

### Browse
- One Object Browser tree: servers, databases, roles, tablespaces, catalogs, extensions, event triggers, publications, schemas, tables,
  views, materialized views, functions, procedures, sequences, types and domains. Every object has Properties and a **Create statement**
  tab. A system switch also shows built-in objects.
- Tables list with statistics: rows, size, index size, dead rows, writes, scans, last vacuum and analyze, with totals and sortable headings.
  Per row: **Details**, **Import** (CSV) and **Export** (CSV, JSON, SQL).
- Server overview with uptime, connections now, work done since the counters were reset, and key settings.
- Partitioned tables: a Partitions tab and tree folder; create, attach and detach partitions.

### SQL runner
- Runs without leaving the page; the result shows ten rows at a time with pages loaded in the background, figures under the result and
  **Export** as CSV, JSON or INSERT statements.
- Database list that starts on the database you came from; tables-and-columns panel (click a table for `SELECT * … LIMIT 10;`, then
  click columns); hints while typing; coloured keywords; a query builder; your own statement history.
- Queries run in a `READ ONLY` transaction enforced by PostgreSQL, with a statement timeout and a row cap.

### Change things safely
- INSERT, UPDATE, DELETE, TRUNCATE, DROP, CREATE, ALTER, GRANT and REVOKE (databases and roles included), each checked first in a transaction
  that is rolled back, shown in a review popup, confirmed with a typed word when it is risky, and recorded in the audit log **before** it is
  committed. Deleted rows can be re-inserted and updates undone from the audit log.
- Table structure: create (with columns, keys, constraints, indexes, options, partitions, owner and privileges), rename, drop, add / edit /
  rename / drop columns, reorder columns, indexes (create, drop, rename, reindex), constraints.
- Inline row editing in the data grid, reviewed and undoable.

### Operate
- **Dashboards**: charts for workload, sessions and locks, storage and WAL, trouble signs and replication, sampled over time, with a live
  strip; the settings that explain the charts have an explanation, a link to the PostgreSQL manual for the server's own version, and (for
  Super Users) a reviewed way to change them with `ALTER SYSTEM`.
- **Monitor** (live sessions, cancel and end), **Alerts & trends** (size history and alerts, sent by webhook or email), **Backups**
  (`pg_dump`, download, restore into a new database, schedules with retention).

### Accounts and access
- Organizations, Super Users, editors and viewers; per-database permissions; employees sign in with their email address (verified by
  email when the installation can send mail); an organization may restrict the addresses its people use.
- Saved server passwords are encrypted at rest. **Server numbers in links and requests are encrypted tokens**
  (`?server=EY1-bUv8Znf_Y2bQpRc`), so the number cannot be read or guessed from an address. Old plain-number links still work; set
  `PGDAS_STRICT_SERVER_IDS=1` to refuse them.
- A plain "Server not found" page when a link names a server that does not exist or that you may not see.

### Upgrading
- This is the first tagged release. Run `python manage.py migrate` after updating from any development copy; the README has the install steps.
- If you used the earlier name PgDeck: the Python package is `pgdas` and the settings are `PGDAS_*` (see the README).
- Changing `PGDAS_SECRET_KEY` (or `PGDAS_ENCRYPTION_KEY`) makes saved passwords unreadable and old server tokens stop working; plain-number
  links keep working unless the strict switch is on.
