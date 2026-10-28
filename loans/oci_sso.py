"""Signing in with OCI IAM (an Oracle identity domain) over OpenID Connect.

Authorization code flow: send the browser to OCI, OCI sends it back to the
callback with a one-time code, we swap the code for an access token
server-to-server, read the user's email from userinfo, and sign in the
existing dashboard account with that email. No account is created here.
"""

import base64
import json
import secrets
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET

TIMEOUT = 15
SCOPE = "openid email profile"
_endpoints = {}


class SsoError(Exception):
    """OCI refused or could not be reached; never contains the secret."""


def _fetch(request):
    try:
        with urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read())
    except HTTPError as err:
        raise SsoError(f"OCI answered {err.code}") from err
    except (URLError, ValueError) as err:
        raise SsoError(f"Could not reach OCI: {err}") from err


def discover():
    if not _endpoints:
        _endpoints.update(_fetch(Request(f"{settings.OCI_SSO_ISSUER_URL}/.well-known/openid-configuration")))
    return _endpoints


def _redirect_uri(request):
    """Where OCI sends the browser back to. It must match, character for
    character, a Redirect URL registered on the OCI application, and the same
    value must be sent in both the authorize and the token request."""
    if settings.OCI_SSO_REDIRECT_URI:
        return settings.OCI_SSO_REDIRECT_URI
    path = reverse("oci_callback")
    return f"{settings.PUBLIC_URL}{path}" if settings.PUBLIC_URL else request.build_absolute_uri(path)


def _fail(reason):
    return redirect(f"{reverse('login')}?sso_error={reason}")


@require_GET
def oci_login(request):
    if not settings.OCI_SSO_ENABLED:
        raise Http404
    state = secrets.token_urlsafe(32)
    request.session["oci_state"] = state
    next_url = request.GET.get("next", "")
    if url_has_allowed_host_and_scheme(next_url, {request.get_host()}, require_https=request.is_secure()):
        request.session["oci_next"] = next_url
    params = urlencode({
        "client_id": settings.OCI_SSO_CLIENT_ID,
        "redirect_uri": _redirect_uri(request),
        "scope": SCOPE,
        "response_type": "code",
        "state": state,
    })
    try:
        return redirect(f"{discover()['authorization_endpoint']}?{params}")
    except SsoError:
        return _fail("unavailable")


def _exchange_code(request, code):
    basic = base64.b64encode(f"{settings.OCI_SSO_CLIENT_ID}:{settings.OCI_SSO_CLIENT_SECRET}".encode()).decode()
    body = urlencode({"grant_type": "authorization_code", "code": code,
                      "redirect_uri": _redirect_uri(request)}).encode()
    token = _fetch(Request(discover()["token_endpoint"], data=body, method="POST", headers={
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
    }))
    return token["access_token"]


def _user_email(access_token):
    info = _fetch(Request(discover()["userinfo_endpoint"],
                          headers={"Authorization": f"Bearer {access_token}"}))
    return (info.get("email") or info.get("preferred_username") or "").strip().lower()


@require_GET
def oci_callback(request):
    expected = request.session.pop("oci_state", None)
    next_url = request.session.pop("oci_next", None) or settings.LOGIN_REDIRECT_URL
    state, code = request.GET.get("state", ""), request.GET.get("code")
    if request.GET.get("error") or not code or not expected or not secrets.compare_digest(state, expected):
        return _fail("expired")

    try:
        email = _user_email(_exchange_code(request, code))
    except (SsoError, KeyError):
        return _fail("unavailable")

    if not email.endswith("@" + settings.PASSWORD_RESET_EMAIL_DOMAIN):
        return _fail("no_account")
    users = list(get_user_model().objects.filter(email__iexact=email, is_active=True)[:2])
    if len(users) != 1:
        return _fail("no_account")

    # Two auth backends are configured, so say which one signed them in.
    login(request, users[0], backend="django.contrib.auth.backends.ModelBackend")
    return redirect(next_url)
