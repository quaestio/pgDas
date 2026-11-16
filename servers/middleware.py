"""Turns the encrypted server token in a request back into the plain number the views work with (see servers/ids.py).

`?server=<token>` and a posted `server` / `server_id` field are replaced in a copy of the request's data before any view or the access
middleware looks at them, so everything after this sees an ordinary number, whether the request carried a token or an old plain number.
A value that is neither stays as it is and is refused (404) by the checks that already exist. JSON bodies are read by the endpoints that take
one, with servers.ids.to_digits.
"""
from django.utils.deprecation import MiddlewareMixin

from . import ids

FIELDS = ("server", "server_id")


class ServerIdMiddleware(MiddlewareMixin):
    def process_request(self, request):
        for attribute in ("GET", "POST"):
            data = getattr(request, attribute, None)
            if data is None or not any(field in data for field in FIELDS):
                continue
            copy = data.copy()
            for field in FIELDS:
                if field in copy:
                    copy.setlist(field, [ids.to_digits(value) for value in copy.getlist(field)])
            setattr(request, attribute, copy)
