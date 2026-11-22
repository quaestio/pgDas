# Changelog

All notable changes to PGDAS, newest first. Versions follow [Semantic Versioning](https://semver.org/): a change that needs
action when you upgrade raises the first number, new features the second, fixes the third.

## Unreleased (will be 1.1.0)

### New
- **Schemas, views, materialized views and sequences** can be created, renamed and dropped from the Object Browser (Super Users), each
  through the same review popup as every other change: **+ New schema** (with an owner), **+ New view**, **+ New materialized view**
  (with or without data), **+ New sequence** (type, start, increment, minimum, maximum, cache, cycle); **Rename** and **Drop** on each page;
  dropping a schema offers CASCADE.
- **New table**: table **access method**, **Of type** (a table made from a composite type), **TOAST** storage parameters, **exclude
  constraints** (also under Add constraint) and a **comment on each constraint**. Constraints and indexes can now be added to **partitioned
  tables** too.
- **SQL runner**: **saved statements** (named, personal, kept per server); hints that understand **table aliases** (`FROM orders o … o.`
  offers the columns of `orders`); "My recent statements" now follows each run without a page reload.
- **Audit log**: an **attach or detach of a partition can be undone** from the audit log (a detach is put back with its old bound).
- **Screenshots** in the README, a **GitHub Actions** workflow that runs the tests on every push, and a Windows installer script
  (`scripts/install-windows.ps1`).

### Fixed
- The SQL runner's tables-and-columns panel disappeared when the sidebar was collapsed.
- Session ages (oldest transaction, longest query) could show a few milliseconds below zero.
- A materialized view created `WITH NO DATA` no longer breaks the preview of its own creation or drop.

### Upgrading
- Run `python manage.py migrate` (one new table for the saved statements).

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
- Changing `PGDAS_SECRET_KEY` (or `PGDAS_ENCRYPTION_KEY`) makes saved passwords unreadable and old server tokens stop working; plain-number
  links keep working unless the strict switch is on.
