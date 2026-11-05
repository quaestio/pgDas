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

Super Users can add and edit employees in their organization. New accounts sign in using email. Name is the display name; there is no separate User ID input. Edit Employee does not expose password fields and ignores submitted password changes. Existing users use Change Password in the header. Legacy usernames continue to work until the account is saved with its email. Email changes do not change passwords. At least one active Super User must remain per organization.

## Assignments

Employees > Database permissions lists the exact server/database assignments and separate read, insert, update, delete, truncate, create, alter, drop, export, backup-create and backup-download grants. Viewers may only receive read/export. Editors default to read/insert/update on assignment; additional actions are explicit. Restore, schedules, role management, settings, monitoring and session control are restricted to Super Users. A new database is unassigned by default. Revoking read revokes all operations on that assignment.

Each employee assignment needs an existing restricted PostgreSQL login. Passwords are encrypted with the application's existing encryption key. This login must not have superuser, createdb, createrole, replication or bypass-RLS attributes, directly or through inherited memberships. PGDAS never falls back to saved administrator credentials for employees. PostgreSQL still checks table/object privileges: PGDAS permission cannot grant a privilege refused by PostgreSQL.

Use roles with only needed object privileges and only intended CONNECT privileges. Database owners must review functions, triggers, rules, operators and SECURITY DEFINER views/functions accessible to employee execution roles. PGDAS is not a sandbox for malicious database code or extensions. Server-wide PostgreSQL catalogs can expose names outside the assigned database through database-owned code; do not grant access to such code.

## Enforcement

Request-scoped checks filter Object Browser, SQL, backups and audit history. Organization scopes constrain saved server management and dashboard/monitoring data. Direct URLs and posted database IDs are checked. Employee history is limited to their own records with a currently assigned database. Legacy unscoped history stays available to organization Super Users.

SQL uses a PostgreSQL parser and exactly one statement per request. Employee SQL accepts supported reads and explicitly granted ordinary data/structure operations. Transaction control, role changes, cross-database facilities, arbitrary functions, unsupported casts and complex privileged ALTER operations are refused. Read-only transactions and restricted PostgreSQL credentials provide additional checks. Unsupported employee SQL may require a Super User. Trigger row-change side effects also require corresponding action grants before commit. Confirmations never grant permission.

Export CSV requires export rights, is row-capped, and escapes spreadsheet formula prefixes. PostgreSQL read privileges alone do not enable the export endpoint. Users who can read data can of course copy what they can see.

## Application database (PostgreSQL)

PGDAS keeps its users, saved servers, audit log and sessions in a PostgreSQL database of its own, separate from the servers it manages. Create the database and its login once, then configure:

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
