from django.conf import settings


def version(request):
    """The release number, for the footer of every page."""
    return {"pgdas_version": settings.PGDAS_VERSION}
