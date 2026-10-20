"""Email through Microsoft Graph, against a stand-in for Microsoft - nothing is sent."""

import io
import json
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import parse_qs

from django.core import mail
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from loans import mail_graph

GRAPH = dict(
    EMAIL_BACKEND="loans.mail_graph.GraphEmailBackend",
    MSGRAPH_TENANT_ID="tenant-123",
    MSGRAPH_CLIENT_ID="client-456",
    MSGRAPH_CLIENT_SECRET="the-secret",
    MSGRAPH_SENDER="no-reply@dash-mfb.com",
)


class Reply:
    def __init__(self, status, body=None):
        self.status = status
        self.body = json.dumps(body).encode() if body is not None else b""

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def refused(status, body):
    return HTTPError("https://example", status, "refused", {}, io.BytesIO(json.dumps(body).encode()))


class FakeMicrosoft:
    """Answers the sign-in with a token and sendMail with 202, recording each call."""

    def __init__(self, send_replies=None):
        self.calls = []
        self.send_replies = list(send_replies or [])
        self.tokens = 0

    def __call__(self, request, timeout=None):
        self.calls.append(request)
        if "login.microsoftonline.com" in request.full_url:
            self.tokens += 1
            return Reply(200, {"access_token": f"token-{self.tokens}", "expires_in": 3600})
        reply = self.send_replies.pop(0) if self.send_replies else Reply(202)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def sends(self):
        return [c for c in self.calls if "graph.microsoft.com" in c.full_url]


@override_settings(**GRAPH)
class GraphEmailTests(SimpleTestCase):
    def setUp(self):
        mail_graph._token.update(value=None, expires=0.0)

    def send(self, fake, **kw):
        with mock.patch.object(mail_graph, "urlopen", fake):
            return mail.send_mail(kw.get("subject", "Reset your password"), kw.get("body", "Click the link"),
                                  None, kw.get("to", ["ada@dash-mfb.com"]))

    def test_it_signs_in_and_sends_from_the_sender_mailbox(self):
        fake = FakeMicrosoft()
        self.assertEqual(self.send(fake), 1)
        sign_in, send = fake.calls
        self.assertEqual(sign_in.full_url, "https://login.microsoftonline.com/tenant-123/oauth2/v2.0/token")
        form = parse_qs(sign_in.data.decode())
        self.assertEqual((form["client_id"], form["client_secret"], form["grant_type"], form["scope"]),
                         (["client-456"], ["the-secret"], ["client_credentials"], ["https://graph.microsoft.com/.default"]))
        self.assertEqual(send.full_url, "https://graph.microsoft.com/v1.0/users/no-reply%40dash-mfb.com/sendMail")
        self.assertEqual(send.get_header("Authorization"), "Bearer token-1")
        message = json.loads(send.data)["message"]
        self.assertEqual(message["subject"], "Reset your password")
        self.assertEqual(message["body"], {"contentType": "Text", "content": "Click the link"})
        self.assertEqual(message["toRecipients"], [{"emailAddress": {"address": "ada@dash-mfb.com"}}])

    def test_the_token_is_reused_until_it_is_near_expiry(self):
        fake = FakeMicrosoft()
        self.send(fake)
        self.send(fake)
        self.assertEqual((fake.tokens, len(fake.sends())), (1, 2))

    def test_an_expired_token_is_renewed_once(self):
        fake = FakeMicrosoft(send_replies=[refused(401, {"error": {"code": "InvalidAuthenticationToken", "message": "expired"}})])
        self.assertEqual(self.send(fake), 1)
        self.assertEqual(fake.tokens, 2)
        self.assertEqual(fake.sends()[-1].get_header("Authorization"), "Bearer token-2")

    def test_a_refusal_says_why_and_never_shows_the_secret(self):
        fake = FakeMicrosoft(send_replies=[refused(403, {"error": {"code": "ErrorAccessDenied",
                                                                   "message": "Access is denied."}})])
        with self.assertRaises(mail_graph.GraphError) as caught:
            self.send(fake)
        self.assertIn("ErrorAccessDenied: Access is denied.", str(caught.exception))
        self.assertNotIn("the-secret", str(caught.exception))

    def test_a_failed_sign_in_says_why(self):
        def wrong_secret(request, timeout=None):
            raise refused(401, {"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret."})
        with self.assertRaises(mail_graph.GraphError) as caught:
            self.send(wrong_secret)
        self.assertIn("AADSTS7000215", str(caught.exception))

    def test_a_missing_setting_is_named(self):
        with self.settings(MSGRAPH_CLIENT_SECRET=""), self.assertRaises(ImproperlyConfigured) as caught:
            self.send(FakeMicrosoft())
        self.assertIn("MSGRAPH_CLIENT_SECRET", str(caught.exception))

    def test_a_recipient_written_with_a_name_is_sent_to_the_address(self):
        fake = FakeMicrosoft()
        self.send(fake, subject="Reset your DashMFB Loan Dashboard password", to=["Ada Obi <ada@dash-mfb.com>"])
        message = json.loads(fake.sends()[0].data)["message"]
        self.assertEqual(message["toRecipients"], [{"emailAddress": {"address": "ada@dash-mfb.com"}}])
