"""A plain-language readiness check: what is set up, what is missing, and how to fix each thing.

Two kinds of checks. ORGANIZATION checks concern what a Super User can see and change (their servers, backups, alert recipients, monitoring).
INSTALLATION checks concern the machine and its .env (HTTPS, email, cron jobs, folders); on a shared installation those belong to the person who
runs PGDAS, so the web page shows them only on a dedicated installation, and `manage.py readiness` shows them on the host.
Each check is {"id", "title", "status": ok | warn | fail | info, "detail", "fix", "scope"}. A check that cannot run reports a warning, never an error.
"""
import os
from datetime import timedelta

from django.conf import settings
from django.core.checks import run_checks
from django.utils import timezone

from . import mode


def _c(ident, title, status, detail, fix="", scope="installation"):
    return {"id": ident, "title": title, "status": status, "detail": detail, "fix": fix, "scope": scope}


def _writable(path):
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".pgdas_write_test"
        probe.write_text("x")
        probe.unlink()
        return True
    except OSError:
        return False


def _scheduled(project, args, every):
    """How to run a PGDAS command on a schedule on this kind of machine."""
    if os.name == "nt":
        return f"Create a Task Scheduler task that runs  {project}\\.venv\\Scripts\\python.exe manage.py {args}  (start in {project}) {every}."
    minutes = {"every minute": "*", "every 10 minutes": "*/10"}[every]
    return f"Add to the crontab of the user PGDAS runs as:  {minutes} * * * * cd {project} && venv/bin/python manage.py {args} >> backups/{args.split()[0]}.log 2>&1"


def _age(moment):
    seconds = (timezone.now() - moment).total_seconds()
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)} minutes ago"
    if seconds < 172800:
        return f"{round(seconds / 3600)} hours ago"
    return f"{round(seconds / 86400)} days ago"


# ---- installation -------------------------------------------------------------------------------------------------------------------------------------

def installation_checks(request=None):
    out = []
    project = str(settings.BASE_DIR)
    out.append(_c("debug", "Production settings", "fail" if settings.DEBUG else "ok",
                  "PGDAS_DEBUG is on: errors show internal details and cookies are not marked secure." if settings.DEBUG else "Debug is off.",
                  "Set PGDAS_DEBUG=0 in .env and restart." if settings.DEBUG else ""))
    insecure = settings.SECRET_KEY.startswith("insecure-development-key")
    out.append(_c("secret", "Secret key", "fail" if insecure else "ok", "The built-in development key is in use." if insecure else "A private secret key is set.",
                  "Generate one: python -c \"from django.core.management.utils import get_random_secret_key as k; print(k())\" and put it in PGDAS_SECRET_KEY." if insecure else ""))
    out.append(_c("encryption", "Encryption key for saved passwords", "ok" if settings.ENCRYPTION_KEY else "info",
                  "A separate key encrypts saved server passwords." if settings.ENCRYPTION_KEY else "Saved passwords are encrypted with a key derived from the secret key. That works, but changing the secret key would make them unreadable.",
                  "" if settings.ENCRYPTION_KEY else "Optional: set PGDAS_ENCRYPTION_KEY to a Fernet key (and keep a copy of it)."))
    hosts = [h for h in settings.ALLOWED_HOSTS if h]
    wildcard = "*" in hosts
    out.append(_c("hosts", "Allowed host names", "warn" if wildcard or not hosts else "ok", "PGDAS answers to any host name." if wildcard else f"PGDAS answers to: {', '.join(hosts)}.",
                  "Set PGDAS_ALLOWED_HOSTS to your real domain name." if wildcard else ""))
    if not settings.DEBUG:
        https = [o for o in settings.CSRF_TRUSTED_ORIGINS if o.startswith("https://")]
        out.append(_c("origins", "HTTPS origins for sign-in forms", "ok" if https else "warn", f"Trusted: {', '.join(https)}." if https else "No https origin is trusted, so sign-in and forms can fail behind a proxy.",
                      "" if https else "Set PGDAS_CSRF_TRUSTED_ORIGINS=https://your.domain in .env."))
        out.append(_c("cookies", "Secure cookies", "ok" if settings.SESSION_COOKIE_SECURE else "warn", "Sign-in cookies are only sent over HTTPS." if settings.SESSION_COOKIE_SECURE else "Cookies can travel over plain HTTP.",
                      "" if settings.SESSION_COOKIE_SECURE else "Remove PGDAS_SECURE_COOKIES=0 (and serve PGDAS over HTTPS)."))
    forwarded = (request.META.get("HTTP_X_FORWARDED_PROTO", "") if request is not None else "")
    if forwarded == "https" and not settings.BEHIND_HTTPS_PROXY:
        out.append(_c("proxy", "HTTPS behind a proxy", "warn", "This page came through an HTTPS proxy, but PGDAS does not trust it, so links in emails will start with http://.",
                      "Set PGDAS_BEHIND_HTTPS_PROXY=1 (and PGDAS_TRUSTED_PROXIES=127.0.0.1) and make nginx send X-Forwarded-Proto."))
    elif settings.BEHIND_HTTPS_PROXY:
        out.append(_c("proxy", "HTTPS behind a proxy", "ok", "PGDAS trusts X-Forwarded-Proto from the proxy, so links and redirects use https."))
    from servers import netpolicy  # noqa: F401  (imported lazily: the module needs settings loaded)
    from access_control.models import Installation
    engine = settings.DATABASES["default"]["ENGINE"]
    sqlite = "sqlite" in engine
    out.append(_c("storage", "Where PGDAS keeps its own data", "warn" if sqlite and not settings.DEBUG else "ok",
                  "One SQLite file: fine for trying it out or one person, but it allows a single writer at a time." if sqlite else "PostgreSQL.",
                  "For several people or organizations set PGDAS_APP_DB_ENGINE=postgresql and the PGDAS_APP_DB_* settings." if sqlite and not settings.DEBUG else ""))
    # email
    if settings.EMAIL_HOST:
        out.append(_c("email", "Email", "ok", f"Mail is sent through {settings.EMAIL_HOST}:{settings.EMAIL_PORT} as {settings.ALERT_EMAIL_FROM}. Use the test button to check that it really arrives.",
                      "", ))
    else:
        out.append(_c("email", "Email", "fail" if mode.is_shared() and settings.ALLOW_ORGANIZATION_REGISTRATION else "warn",
                      "No mail server is set: registration, employee invitations, email verification and alert emails cannot be sent.",
                      "Set PGDAS_EMAIL_HOST, PGDAS_EMAIL_PORT, PGDAS_EMAIL_USER, PGDAS_EMAIL_PASSWORD (and PGDAS_EMAIL_TLS=1 or PGDAS_EMAIL_SSL=1) in .env."))
    out.append(_c("public_url", "Address used in emailed links", "ok" if settings.PUBLIC_URL else "info",
                  f"Links in alert emails point to {settings.PUBLIC_URL}." if settings.PUBLIC_URL else "Not set: PGDAS works it out from the allowed hosts, which is right most of the time.",
                  "" if settings.PUBLIC_URL else "Set PGDAS_PUBLIC_URL=https://your.domain to be sure."))
    # folders and tools
    data_dir = settings.BASE_DIR / "data"
    out.append(_c("data_dir", "Data folder", "ok" if _writable(data_dir) else "fail", f"{data_dir} can be written." if _writable(data_dir) else f"{data_dir} cannot be written by the user PGDAS runs as.",
                  "" if _writable(data_dir) else f"As that user: sudo chown -R <user>:<user> {data_dir}"))
    backup_dir = settings.BACKUP_DIR
    out.append(_c("backup_dir", "Backup folder", "ok" if _writable(backup_dir) else "fail", "Backup files can be written." if _writable(backup_dir) else "The backup folder cannot be written by the user PGDAS runs as.",
                  "" if _writable(backup_dir) else f"Create it and give it to that user: sudo chown -R <user>:<user> {backup_dir}"))
    try:
        from backups import dump
        dump.tool("pg_dump")
        out.append(_c("pg_dump", "pg_dump (for backups)", "ok", "Found."))
    except Exception as error:
        out.append(_c("pg_dump", "pg_dump (for backups)", "warn", f"Not found: {error}", "Install postgresql-client (Ubuntu: sudo apt install postgresql-client) or set PGDAS_PG_BIN to the folder holding pg_dump."))
    # cron jobs
    from watch.models import WatchRun
    last = WatchRun.objects.order_by("-started_at").first()
    limit = timedelta(seconds=max(300, int(settings.WATCH_INTERVAL_SECONDS)) * 3)
    if last is None:
        out.append(_c("run_watch", "Monitoring sampler (run_watch)", "warn", "It has never run, so Dashboards stay empty and alerts are not sent.",
                      _scheduled(project, "run_watch", "every minute")))
    elif timezone.now() - last.started_at > limit:
        out.append(_c("run_watch", "Monitoring sampler (run_watch)", "warn", f"It last ran {_age(last.started_at)}; it should run every minute.", "Check the cron job (crontab -l) and backups/run_watch.log."))
    else:
        out.append(_c("run_watch", "Monitoring sampler (run_watch)", "ok", f"Last run {_age(last.started_at)}: {last.sampled} sampled, {last.failed} failed, {last.skipped} skipped."))
    from backups.models import Backup, BackupSchedule
    schedules = BackupSchedule.objects.count()
    if schedules:
        recent = Backup.objects.filter(kind="scheduled").order_by("-started_at").first() if hasattr(Backup, "started_at") else Backup.objects.filter(kind="scheduled").order_by("-pk").first()
        when = getattr(recent, "started_at", None) or getattr(recent, "created_at", None)
        if recent is None:
            out.append(_c("run_backups", "Scheduled backups (run_backups)", "warn", f"{schedules} schedule(s) exist but no scheduled backup was ever made.",
                          _scheduled(project, "run_backups", "every 10 minutes")))
        else:
            out.append(_c("run_backups", "Scheduled backups (run_backups)", "ok" if recent.status == "ok" else "warn",
                          f"The last scheduled backup {'worked' if recent.status == 'ok' else 'failed'}" + (f" ({_age(when)})." if when else "."),
                          "" if recent.status == "ok" else "Open Backups to see the reason."))
    # mode, registration, terms
    if mode.is_shared():
        out.append(_c("mode", "Shared installation", "info", "Many organizations; each sees only its own servers, people and alerts. Registration is " + ("open." if mode.registration_open() else "closed.")))
        if settings.ALLOW_ORGANIZATION_REGISTRATION and not settings.TERMS_URL:
            out.append(_c("terms", "Terms and Conditions", "info", "People registering accept the built-in terms page, which is a starting point only.", "Have your own organization review it, or set PGDAS_TERMS_URL to your own terms."))
    out.append(_c("version", "Version", "info", f"PGDAS {settings.PGDAS_VERSION}."))
    problems = [c for c in run_checks() if c.level >= 30]
    for problem in problems:
        out.append(_c(f"check-{problem.id}", f"System check {problem.id}", "fail" if problem.level >= 40 else "warn", problem.msg, problem.hint or ""))
    return out


# ---- an organization ----------------------------------------------------------------------------------------------------------------------------------

def organization_checks(organization, user=None):
    from access_control.models import OrganizationServer
    from servers.models import ServerConnection
    from watch import notify
    from watch.models import ServerHealth
    out = []
    servers = list(ServerConnection.objects.filter(organization_scope__organization=organization).select_related("health"))
    if not servers:
        out.append(_c("servers", "Servers", "warn", "No PostgreSQL server is saved yet.", "On the home page press Add server, give the connection details and press Test.", "organization"))
    else:
        out.append(_c("servers", "Servers", "ok", f"{len(servers)} saved: {', '.join(s.name for s in servers[:6])}{'…' if len(servers) > 6 else ''}.", "", "organization"))
        locked = [s.name for s in servers if s.asks_for_secret and not s.unlocked_until]
        if locked:
            out.append(_c("locked", "Locked servers", "warn", f"These servers keep no password or key passphrase and are locked, so their backups and monitoring are paused: {', '.join(locked)}.",
                          "Press Unlock next to the server on the home page and enter it.", "organization"))
        failing = [f"{s.name} ({s.health.last_error[:80]})" for s in servers if getattr(s, "health", None) and s.health.failures]
        if failing:
            out.append(_c("unreachable", "Servers that did not answer", "warn", "The monitoring sampler could not reach: " + "; ".join(failing) + ".",
                          "Open the server and press Test connection: the message says what to change (address, firewall, password).", "organization"))
        never = [s.name for s in servers if not getattr(s, "health", None) or not s.health.sample_count]
        if len(never) == len(servers):
            out.append(_c("monitoring", "Monitoring", "warn", "No server has been sampled yet, so the Dashboards are empty.",
                          "Press Sample now on the Dashboards page. If it never fills in by itself, the person who runs PGDAS must run `manage.py run_watch` every minute.", "organization"))
        else:
            out.append(_c("monitoring", "Monitoring", "ok", "Servers are being sampled; see Dashboards.", "", "organization"))
    from backups.models import BackupSchedule
    if servers:
        has_schedule = BackupSchedule.objects.filter(server__in=servers).exists()
        out.append(_c("backups", "Backups", "ok" if has_schedule else "warn",
                      "A backup schedule exists." if has_schedule else "No backup schedule is set, so nothing is backed up automatically.",
                      "" if has_schedule else "Open Backups, press Schedule and choose how often and how many to keep.", "organization"))
    chosen = notify.channels_for_organization(organization)
    where = {"organization": "your organization's own recipients and webhook", "installation": "the installation's list (your organization has none of its own)", "none": ""}[chosen["source"]]
    out.append(_c("alerts", "Alert notifications", "ok" if chosen["source"] != "none" else "warn",
                  f"Alerts go to {where}." if where else "Nobody receives alert messages: problems show inside PGDAS only.",
                  "" if chosen["source"] != "none" else "Open Alerts & trends, then Alert notifications, and add an email address or a webhook.", "organization"))
    if user is not None:
        out.append(_c("account_email", "Your email address", "ok" if getattr(user, "email_verified", False) else "warn",
                      "Your address is verified." if getattr(user, "email_verified", False) else "Your address is not verified yet.",
                      "" if getattr(user, "email_verified", False) else "Sign in again and use the link that is emailed to you.", "organization"))
    return out


def summary(checks):
    counts = {"ok": 0, "info": 0, "warn": 0, "fail": 0}
    for check in checks:
        counts[check["status"]] = counts.get(check["status"], 0) + 1
    return counts