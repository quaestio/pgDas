from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class EmployeeBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None or password is None:
            return None
        User = get_user_model()
        user = User.objects.filter(username=username).first()
        if user is None:
            matches = list(User.objects.filter(email__iexact=username).exclude(email="")[:2])
            user = matches[0] if len(matches) == 1 else None
        if user is None:
            User().set_password(password)
        elif user.check_password(password) and self.user_can_authenticate(user):
            return user
