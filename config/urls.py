from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.http import HttpResponse
from django.urls import include, path

from loans import oci_sso
from loans.password_reset import ResetConfirmView, ResetRequestView


def robots_txt(request):
    """Disallow every crawler across the whole site."""
    return HttpResponse("User-agent: *\nDisallow: /\n", content_type="text/plain")


urlpatterns = [
    path("robots.txt", robots_txt),
    path("admin/", admin.site.urls),
    path(
        "login/",
        auth_views.LoginView.as_view(
            template_name="registration/login.html", redirect_authenticated_user=True,
            extra_context={"oci_sso_enabled": settings.OCI_SSO_ENABLED},
        ),
        name="login",
    ),
    path("login/oci/", oci_sso.oci_login, name="oci_login"),
    path("login/sso/callback/", oci_sso.oci_callback, name="oci_callback"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    # Forgot password: request a link, "check your email", set a new one, done.
    path("password-reset/", ResetRequestView.as_view(), name="password_reset"),
    path("password-reset/sent/", auth_views.PasswordResetDoneView.as_view(), name="password_reset_done"),
    path("password-reset/done/", auth_views.PasswordResetCompleteView.as_view(), name="password_reset_complete"),
    path("password-reset/<uidb64>/<token>/", ResetConfirmView.as_view(), name="password_reset_confirm"),
    path("", include("loans.urls")),
]
