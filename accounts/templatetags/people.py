import re

from django import template

register = template.Library()


@register.filter
def initials(name):
    """The letters in a person's avatar: the first letters of the first and last word of the name (or of an email's part before the @)."""
    text = (name or "").strip()
    if "@" in text:
        text = text.split("@", 1)[0]
    words = [word for word in re.split(r"[\s._+\-]+", text) if word]
    if not words:
        return "?"
    return (words[0][0] + (words[-1][0] if len(words) > 1 else "")).upper()
