from django.contrib import admin
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.urls import path


def health(request):
    return JsonResponse({"status": "ok"})


def ready(request):
    return JsonResponse({"status": "ready"})


def login_view(request):
    if request.method == "POST":
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            return redirect("me")
    else:
        form = AuthenticationForm(request)

    return render(request, "login.html", {"form": form})


@login_required
def me_view(request):
    return render(request, "me.html")


@login_required
def logout_view(request):
    logout(request)
    return redirect("login")


def demo_guard(request):
    if request.user.is_authenticated and request.user.username == "demo":
        return HttpResponseForbidden("Demo user is read-only.")
    return None


urlpatterns = [
    path("admin/", admin.site.urls),
    path("health", health),
    path("ready", ready),
    path("login/", login_view, name="login"),
    path("logout/", logout_view, name="logout"),
    path("me/", me_view, name="me"),
]
