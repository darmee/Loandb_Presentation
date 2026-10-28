"""Forgot password: a one-time link sent by email, to company addresses only.

Django's own password reset does the hard parts - the signed, single-use
token, the expiry, never saying whether an address has an account. This adds
the dashboard's rules on top:

* Only @dash-mfb.com addresses (PASSWORD_RESET_EMAIL_DOMAIN). Anything else
  is refused on the form, and an account whose saved address is on another
  domain is never sent a link, even if an admin typed one in.
* At most RESET_LIMITS requests an hour per address and per computer, so the
  form cannot be used to flood someone's inbox.
* The link points at DJANGO_PUBLIC_URL when set, not at whatever host the
  request arrived on - behind IIS that can be 127.0.0.1:8600.
* Setting a new password lifts a sign-in lockout on that account: someone
  locked out for too many wrong passwords is exactly who resets it.
"""

import logging
from urllib.parse import urlsplit

from axes.utils import reset as reset_lockout
from django import forms
from django.conf import settings
from django.contrib.auth import forms as auth_forms
from django.contrib.auth import views as auth_views
from django.core.cache import cache
from django.http import HttpResponseRedirect

audit = logging.getLogger("loans.audit")

RESET_WINDOW = 60 * 60                       # seconds
RESET_LIMITS = {"email": 3, "ip": 20}        # requests per RESET_WINDOW


def company_address(email):
    return (email or "").strip().lower().endswith("@" + settings.PASSWORD_RESET_EMAIL_DOMAIN)


class CompanyEmailResetForm(auth_forms.PasswordResetForm):
    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        if not company_address(email):
            raise forms.ValidationError(f"Use your @{settings.PASSWORD_RESET_EMAIL_DOMAIN} email address.")
        return email

    def get_users(self, email):
        return (user for user in super().get_users(email) if company_address(user.email))


def _over_limit(kind, value):
    key = f"password-reset:{kind}:{value}"
    cache.add(key, 0, RESET_WINDOW)
    try:
        count = cache.incr(key)
    except ValueError:              # expired between the two calls
        cache.set(key, 1, RESET_WINDOW)
        count = 1
    return count > RESET_LIMITS[kind]


class ResetRequestView(auth_views.PasswordResetView):
    form_class = CompanyEmailResetForm
    email_template_name = "registration/password_reset_email.txt"
    extra_email_context = {"site_name": "DashMFB Loan Dashboard", "hours": settings.PASSWORD_RESET_TIMEOUT // 3600}

    def get_context_data(self, **kwargs):
        return super().get_context_data(company_domain=settings.PASSWORD_RESET_EMAIL_DOMAIN, **kwargs)

    def form_valid(self, form):
        email = form.cleaned_data["email"].lower()
        ip = self.request.META.get("REMOTE_ADDR", "?")
        # Either way the page says the same thing, so it gives away nothing.
        if _over_limit("email", email) or _over_limit("ip", ip):
            audit.warning("password reset refused, too many requests: email=%s ip=%s", email, ip)
            return HttpResponseRedirect(self.get_success_url())
        audit.info("password reset requested: email=%s ip=%s", email, ip)
        if not settings.EMAIL_PROVIDER:
            audit.warning("password reset email NOT SENT: no email provider in .env (MSGRAPH_* or "
                          "EMAIL_HOST), so it was printed to the server console instead. "
                          "See 'Forgot password' in README.md.")
        public = urlsplit(settings.PUBLIC_URL) if settings.PUBLIC_URL else None
        form.save(
            domain_override=public.netloc if public else None,
            use_https=public.scheme == "https" if public else self.request.is_secure(),
            token_generator=self.token_generator,
            from_email=self.from_email,
            email_template_name=self.email_template_name,
            subject_template_name=self.subject_template_name,
            request=self.request,
            extra_email_context=self.extra_email_context,
        )
        return HttpResponseRedirect(self.get_success_url())


class ResetConfirmView(auth_views.PasswordResetConfirmView):
    def form_valid(self, form):
        response = super().form_valid(form)
        username = form.user.get_username()
        reset_lockout(username=username)
        audit.info("password reset completed: user=%s ip=%s", username, self.request.META.get("REMOTE_ADDR", "?"))
        return response
