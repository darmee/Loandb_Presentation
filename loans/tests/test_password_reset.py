"""Forgot password: a one-time link by email, to @dash-mfb.com addresses only."""

import re

from axes.models import AccessAttempt
from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

PASSWORD = "a-long-test-password"
NEW_PASSWORD = "Another-long-one-42"


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    PASSWORD_RESET_EMAIL_DOMAIN="dash-mfb.com",
    PUBLIC_URL="",
)
class PasswordResetTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("ada", email="Ada@dash-mfb.com", password=PASSWORD)

    def ask(self, email):
        return self.client.post(reverse("password_reset"), {"email": email})

    def link(self):
        return re.search(r"https?://\S+", mail.outbox[-1].body).group(0)

    def test_the_sign_in_page_links_to_it(self):
        self.assertContains(self.client.get(reverse("login")), reverse("password_reset"))

    def test_a_company_address_with_an_account_gets_a_link(self):
        response = self.ask("ada@DASH-MFB.com")
        self.assertRedirects(response, reverse("password_reset_done"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["Ada@dash-mfb.com"])
        self.assertIn("/password-reset/", self.link())

    def test_any_other_domain_is_refused_on_the_form(self):
        for email in ("ada@gmail.com", "ada@dash-mfb.com.evil.com", "ada@mail.dash-mfb.com"):
            with self.subTest(email=email):
                response = self.ask(email)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Use your @dash-mfb.com email address.")
        self.assertEqual(len(mail.outbox), 0)

    def test_an_unknown_address_looks_the_same_but_sends_nothing(self):
        response = self.ask("nobody@dash-mfb.com")
        self.assertRedirects(response, reverse("password_reset_done"))
        self.assertEqual(len(mail.outbox), 0)

    def test_an_account_saved_with_another_domain_is_never_sent_a_link(self):
        User.objects.create_user("bo", email="bo@gmail.com", password=PASSWORD)
        self.ask("bo@gmail.com")
        self.assertEqual(len(mail.outbox), 0)

    def test_the_link_sets_a_new_password_once(self):
        self.ask("ada@dash-mfb.com")
        form_page = self.client.get(self.link(), follow=True)
        self.assertContains(form_page, "Set a new password")
        response = self.client.post(form_page.redirect_chain[-1][0],
                                    {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD})
        self.assertRedirects(response, reverse("password_reset_complete"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(NEW_PASSWORD))
        # Used once, the link is dead.
        self.client.logout()
        self.assertContains(self.client.get(self.link(), follow=True), "This link no longer works")

    def test_a_weak_password_is_refused(self):
        self.ask("ada@dash-mfb.com")
        form_page = self.client.get(self.link(), follow=True)
        response = self.client.post(form_page.redirect_chain[-1][0], {"new_password1": "12345678", "new_password2": "12345678"})
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password(PASSWORD))

    def test_too_many_requests_send_nothing_more(self):
        for _ in range(5):
            self.ask("ada@dash-mfb.com")
        self.assertEqual(len(mail.outbox), 3)

    def test_the_link_uses_the_public_address_when_set(self):
        with self.settings(PUBLIC_URL="https://loans.dash-mfb.com"):
            self.ask("ada@dash-mfb.com")
        self.assertTrue(self.link().startswith("https://loans.dash-mfb.com/password-reset/"))

    def test_a_new_password_lifts_a_sign_in_lockout(self):
        for _ in range(5):
            self.client.post(reverse("login"), {"username": "ada", "password": "wrong"}, REMOTE_ADDR="10.0.0.9")
        self.assertTrue(AccessAttempt.objects.filter(username="ada").exists())
        self.ask("ada@dash-mfb.com")
        form_page = self.client.get(self.link(), follow=True)
        self.client.post(form_page.redirect_chain[-1][0], {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD})
        self.assertFalse(AccessAttempt.objects.filter(username="ada").exists())
        response = self.client.post(reverse("login"), {"username": "ada", "password": NEW_PASSWORD}, REMOTE_ADDR="10.0.0.9")
        self.assertRedirects(response, reverse("presentation"), fetch_redirect_response=False)

    def test_the_log_says_so_when_no_email_provider_is_set(self):
        with self.settings(EMAIL_PROVIDER=""), self.assertLogs("loans.audit", "WARNING") as logged:
            self.ask("ada@dash-mfb.com")
        self.assertIn("NOT SENT: no email provider", "\n".join(logged.output))
        with self.settings(EMAIL_PROVIDER="graph"), self.assertNoLogs("loans.audit", "WARNING"):
            self.ask("ada@dash-mfb.com")
