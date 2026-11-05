"""The signed-in person's own profile: who they are in PGDAS, and their name. Designation, email and role are set by a Super User."""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from access_control.policy import membership_for
from audit.models import record


class ProfileForm(forms.Form):
    name = forms.CharField(max_length=150, label="Name")


@login_required
def profile(request):
    user = request.user
    form = ProfileForm(request.POST or None, initial={"name": user.get_full_name()})
    if request.method == "POST" and form.is_valid():
        user.first_name, user.last_name = form.cleaned_data["name"].strip(), ""       # one "name" field, as on the Employees form
        user.save(update_fields=["first_name", "last_name"])
        record(user, "profile.update", None, "Changed their own name")
        messages.success(request, "Your name was saved.")
        return redirect("profile")
    return render(request, "accounts/profile.html", {"form": form, "member": membership_for(user)})
