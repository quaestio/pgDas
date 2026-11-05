from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class EmployeeBackend(ModelBackend):
    """Sign in with an email address and a password. There is no username to sign in with."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None or password is None:
            return None
        User = get_user_model()
        address = str(username).strip().lower()
        user = User.objects.filter(email=address).exclude(email="").first() if "@" in address else None
        if user is None:
            User().set_password(password)           # same work whether or not the address exists
        elif user.check_password(password) and self.user_can_authenticate(user):
            return user