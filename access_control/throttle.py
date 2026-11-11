"""Sign-in throttling: too many failed attempts lock an email address, or a network address, for a while.

Failures are counted per email address as typed (whether or not an account has it, so the answer never says which addresses exist)
and per network address. When a count in the last PGDAS_SIGNIN_LOCKOUT_MINUTES reaches its limit, further attempts are refused,
even with the right password, until enough old failures have aged out. A successful sign-in clears that email address's failures.
Kept in the database, so it survives restarts and works across processes.
"""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from audit.models import record

from .models import SignInFailure


def _window_start():
    return timezone.now() - timedelta(minutes=settings.SIGNIN_LOCKOUT_MINUTES)


def _minutes_left(times, limit):
    """Whole minutes (at least 1) until the count falls below the limit, or 0 when it is below it now."""
    times = sorted(times)
    if not limit or len(times) < limit:
        return 0
    frees_at = times[len(times) - limit] + timedelta(minutes=settings.SIGNIN_LOCKOUT_MINUTES)
    return max(1, int((frees_at - timezone.now()).total_seconds() // 60) + 1)


def minutes_locked(email, address):
    """0 when this attempt may go ahead, otherwise the minutes to wait."""
    since = _window_start()
    waits = [_minutes_left(SignInFailure.objects.filter(email=email, at__gte=since).values_list("at", flat=True), settings.SIGNIN_MAX_FAILURES)]
    if address:
        waits.append(_minutes_left(SignInFailure.objects.filter(address=address, at__gte=since).values_list("at", flat=True),
                                   settings.SIGNIN_MAX_FAILURES_PER_ADDRESS))
    return max(waits)


def record_failure(email, address):
    SignInFailure.objects.filter(at__lt=timezone.now() - timedelta(days=1)).delete()          # nothing older than a day is ever needed
    SignInFailure.objects.create(email=email[:254], address=address)
    since = _window_start()
    if settings.SIGNIN_MAX_FAILURES and SignInFailure.objects.filter(email=email, at__gte=since).count() == settings.SIGNIN_MAX_FAILURES:
        record(None, "access.signin_locked", None, f"{email[:100]} locked for {settings.SIGNIN_LOCKOUT_MINUTES} minutes after "
               f"{settings.SIGNIN_MAX_FAILURES} failed sign-ins from {address or 'an unknown address'}", ok=False)


def clear(email):
    SignInFailure.objects.filter(email=email).delete()