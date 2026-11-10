# PGDAS accounts and access control

PGDAS uses a custom accounts.User model. Existing user IDs, password hashes, sessions and foreign keys are preserved through the legacy auth_user table name. This is not Django's default user model. Django admin is not installed or routed.

## Installation

1. Install requirements and configure the application database.
2. Run `python manage.py migrate`.
3. Run `python manage.py installation_token` on the host.
4. Open `/setup/`, enter the token, organization, name, designation, email and password.
5. Setup permanently closes after the initial Super User is created. Token issuance then refuses further requests. Use `python manage.py changepassword <email>` for host-side recovery.

For this existing installation, an additive migration assigned existing users and servers to My organization. Existing PostgreSQL connections and passwords were retained. Rename the organization under Employees > Organization settings. Existing Django superusers became PGDAS Super Users; legacy staff became Editors, and other users became Viewers. No database grants were inferred for employees.

## Employees

Super Users can add and edit employees in their organization. **Email is the only sign-in ID**: there is no username. An address is unique across the whole installation (capital letters do not count), so no two people, in any organization, share one. Name is the display name. Edit Employee does not expose password fields and ignores submitted password changes. Existing users use Change Password in the header. Email changes do not change passwords. At least one active Super User must remain per organization.

An account that has no email address cannot sign in. `python manage.py accounts_without_email` lists them; a Super User gives each one an address under Employees > Edit. The migration that introduced this rule lower-cases every address, copies an address that was used as a username into the email field, and **stops and lists the accounts** if two of them would share an address; fix those and migrate again.

## Organizations

The first organization is created at `/setup/` together with its first Super User. Every organization always has at least one active Super User: the last one cannot be demoted, deactivated or deleted, and a new organization is created in one step with its first Super User. If an organization somehow has none, `python manage.py check` warns, and `python manage.py create_superuser --organization "<name>"` (run on the server host) adds one.

### Dedicated and shared installations

`PGDAS_MODE` in `.env` (nothing in the app can change it; restart to apply) decides how many organizations the installation serves:

- **dedicated** (default): exactly one organization, created at `/setup/`. Its people are added under Employees. There is no registering or creating of other organizations (those pages answer 404 or 403), and the "Restrict organization creation" switch is not shown. If an installation set to dedicated nevertheless holds several organizations (the mode was changed later), `manage.py check` warns and the Organization settings page shows a warning; they keep working, only creating more is switched off.
- **shared**: many organizations, each with its own people, servers, backups and audit log, completely separate; a person belongs to one organization and signs in with an email address that is unique across all of them. The signed-in person's organization name is shown in the header. New organizations come from the Create organization form of a Super User whose organization allows it (below) and, when switched on, from the public page `/register/`.

Public registration (shared only; `PGDAS_ALLOW_ORGANIZATION_REGISTRATION=1`, off by default): a visitor enters an organization name and their own details and becomes the first Super User of the new organization. A "Register your organization" link then appears on the sign-in page. It is protected by a limit of `PGDAS_REGISTRATIONS_PER_HOUR` attempts per address (counted in the server's memory, so per process), a hidden field that catches scripts, the usual password rules, and an optional cap `PGDAS_MAX_ORGANIZATIONS` on the number of organizations (unconfirmed registrations count). When the installation can send email (`PGDAS_EMAIL_HOST`), the organization and its Super User are created switched off and a link emailed to the address, valid for `PGDAS_REGISTRATION_CONFIRM_HOURS` hours, switches them on; without email they are active at once, so switch registration on only when that is acceptable. Registration is part of the sign-in page (a second tab, `Register your organization`), with a box of cautions (first Super User's responsibility, everything is recorded, changes to connected databases are real, least-privilege logins, email is the sign-in ID, strong password) and a required **Terms and Conditions** acceptance. The built-in terms are at `/terms/` (readable without signing in; a starting point, not legal advice); an operator with terms of their own sets `PGDAS_TERMS_URL` and the page links those instead. What was accepted (version, address, time) is stored in the audit record of the registration; change `VERSION` in `accounts/terms.py` when the built-in text changes. The sign-in page also carries a short notice that use is authorised-only and recorded. A visitor who tries an address that is already in use is told so, which reveals that the address exists; the attempt limit keeps that from being scraped.

`python manage.py organizations` (run on the server host: nobody inside PGDAS sees across organizations) lists the organizations with their numbers of people, Super Users and servers, `--disable "<name>"` / `--enable "<name>"` switches one off or on (its people can no longer use PGDAS), and `--purge-pending <hours>` removes registrations whose email link was never opened.

### Organization settings
Organizations are fully separate: a Super User sees nothing of another organization, including one they created. Under Employees > Organization settings a Super User sets:

- **Restrict organization creation** (shared installations only; on by default, also for every new organization). On: this organization's Super Users cannot create organizations. Off: they can, with **Create another organization** (organization name plus its first Super User, saved together). One installation can therefore have organizations that may create others and organizations that may not.
- **Allowed IP addresses** (optional): one address or range per line (`203.0.113.7`, `10.0.0.0/24`, IPv4 or IPv6, `#` for comments). Empty means no restriction. When set, **everyone except Super Users** can use PGDAS only from those addresses. It is checked on every request after sign-in: someone outside the list is signed out at once, sees "Access from this address is not allowed", and the refusal is recorded in the audit log with the address. **Super Users can always sign in from any address**, so a wrong list cannot lock everybody out.

Behind a reverse proxy PGDAS sees the proxy's address. List the proxies in `PGDAS_TRUSTED_PROXIES` (comma separated addresses or ranges) and the `X-Forwarded-For` header is then believed, but only from them, and the visitor is the first address from the right that is not itself a trusted proxy, so a forged header does not help. Without that setting the header is ignored.

## Assignments

Employees > Database permissions lists the exact server/database assignments and separate read, insert, update, delete, truncate, create, alter, drop, export, backup-create and backup-download grants. Viewers may only receive read/export. Editors default to read/insert/update on assignment; additional actions are explicit. Restore, schedules, role management, settings, monitoring and session control are restricted to Super Users. A new database is unassigned by default. Revoking read revokes all operations on that assignment.

Each employee assignment needs an existing restricted PostgreSQL login. Passwords are encrypted with the application's existing encryption key. This login must not have superuser, createdb, createrole, replication or bypass-RLS attributes, directly or through inherited memberships. PGDAS never falls back to saved administrator credentials for employees. PostgreSQL still checks table/object privileges: PGDAS permission cannot grant a privilege refused by PostgreSQL.

Use roles with only needed object privileges and only intended CONNECT privileges. Database owners must review functions, triggers, rules, operators and SECURITY DEFINER views/functions accessible to employee execution roles. PGDAS is not a sandbox for malicious database code or extensions. Server-wide PostgreSQL catalogs can expose names outside the assigned database through database-owned code; do not grant access to such code.

## Enforcement

Request-scoped checks filter Object Browser, SQL, backups and audit history. Organization scopes constrain saved server management and dashboard/monitoring data. Direct URLs and posted database IDs are checked. Employee history is limited to their own records with a currently assigned database. Legacy unscoped history stays available to organization Super Users.

SQL uses a PostgreSQL parser and exactly one statement per request. Employee SQL accepts supported reads and explicitly granted ordinary data/structure operations. Transaction control, role changes, cross-database facilities, arbitrary functions, unsupported casts and complex privileged ALTER operations are refused. Read-only transactions and restricted PostgreSQL credentials provide additional checks. Unsupported employee SQL may require a Super User. Trigger row-change side effects also require corresponding action grants before commit. Confirmations never grant permission.

Export CSV requires export rights, is row-capped, and escapes spreadsheet formula prefixes. PostgreSQL read privileges alone do not enable the export endpoint. Users who can read data can of course copy what they can see.

## Application database (PostgreSQL or SQLite)

PGDAS keeps its users, saved servers, audit log and sessions in a database of its own, separate from the servers it manages. `PGDAS_APP_DB_ENGINE` chooses it: `postgresql` (default, recommended for any shared installation) or `sqlite` (one file, `PGDAS_SQLITE_PATH`; for trying PGDAS out or one person, because SQLite allows one writer at a time). One installation uses one engine; every migration is tested on both. For PostgreSQL, create the database and its login once, then configure:

```
PGDAS_APP_DB_NAME=pgdas_identity
PGDAS_APP_DB_USER=pgdas_app
PGDAS_APP_DB_PASSWORD=<set securely>
PGDAS_APP_DB_HOST=localhost
PGDAS_APP_DB_PORT=5432
PGDAS_APP_DB_SSLMODE=prefer
```

Provision this database separately from managed databases, then run `python manage.py migrate`. For remote connections use verified TLS (`verify-full`). The login needs `CREATEDB` only to run the automated tests.

To move to another PostgreSQL server, stop web/background writers, back up the database, export with `python manage.py dumpdata --natural-foreign --exclude contenttypes --exclude auth.permission --exclude sessions --indent 2 --output data/application-transfer.json`, configure the destination, run migrate, then loaddata. The migration creates Installation(pk=1); loaddata updates it. Preserve PGDAS_SECRET_KEY / PGDAS_ENCRYPTION_KEY and separately copy backup files. Protect the transfer file: it contains password hashes and encrypted connection credentials. Validate row counts, login, assignments and a test connection before restarting writers. Existing sessions require signing in again because sessions are not transferred.
## Upgrade another existing installation

Back up first. The custom user model intentionally keeps the legacy physical table and IDs. Before switching an old installation to the new model, set PGDAS_LEGACY_UPGRADE=1, run `python manage.py migrate accounts 0001 --fake-initial --skip-checks`, remove PGDAS_LEGACY_UPGRADE, then run `python manage.py migrate`. Do not leave transitional mode enabled while serving requests. Fresh installations do not use transitional mode. The local installation was already upgraded.

## Validation

Automated tests cover setup tokens/locking, employee creation/editing, password preservation, email login, last-administrator protection, cross-organization denial, assignment revocation, SQL transaction escapes, direct endpoint denial and audit atomicity. PostgreSQL integration checks use disposable scratch databases/roles. Desktop/mobile UI checks run against a copy of the application database, preserving live user data.
