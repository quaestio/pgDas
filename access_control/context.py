from . import mode


def installation(request):
    """For the templates: whether this is a shared installation, and the signed-in person's organization (shown only when shared)."""
    member = getattr(request, "pgdas_member", None)
    return {"pgdas_shared": mode.is_shared(), "pgdas_organization": member.organization.name if member is not None else ""}