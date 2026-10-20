"""Sending email through Microsoft Graph instead of SMTP.

Django hands every email to an "email backend"; this one posts it to Graph's
sendMail endpoint, so the reset emails go out from a Microsoft 365 mailbox
without SMTP being switched on for it.

It signs in as an app registered in Entra ID (Azure AD) - the client
credentials flow: tenant ID, client (application) ID and a client secret, all
from .env - and sends from MSGRAPH_SENDER. The app needs the Microsoft Graph
*application* permission Mail.Send, with admin consent. That permission lets
the app send as any mailbox in the tenant, so restrict it to the one sender
mailbox in Exchange Online (an application access policy). See README.md,
"Forgot password".

Only the standard library is used, so there is nothing extra to install on
the server.
"""

import json
import threading
import time
from email.utils import parseaddr
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.mail.backends.base import BaseEmailBackend

TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
SEND_URL = "https://graph.microsoft.com/v1.0/users/{sender}/sendMail"
SCOPE = "https://graph.microsoft.com/.default"
TIMEOUT = 15            # seconds, per request
EARLY = 120             # renew a token this many seconds before it expires

_token = {"value": None, "expires": 0.0}
_token_lock = threading.Lock()


class GraphError(Exception):
    """Graph or the sign-in refused; the message says why, never the secret."""


def _call(request):
    """Make a request; return (status, parsed JSON body or {})."""
    try:
        with urlopen(request, timeout=TIMEOUT) as response:
            body = response.read()
            return response.status, (json.loads(body) if body else {})
    except HTTPError as err:
        body = err.read()
        try:
            detail = json.loads(body) if body else {}
        except ValueError:
            detail = {}
        return err.code, detail
    except URLError as err:
        raise GraphError(f"Could not reach Microsoft: {err.reason}") from err


def _reason(detail):
    """The human part of a Microsoft error response."""
    error = detail.get("error")
    if isinstance(error, dict):            # Graph: {"error": {"code", "message"}}
        return f"{error.get('code', '')}: {error.get('message', '')}".strip(": ")
    return detail.get("error_description") or error or "no reason given"


class GraphEmailBackend(BaseEmailBackend):
    def __init__(self, fail_silently=False, **kwargs):
        super().__init__(fail_silently=fail_silently, **kwargs)
        self.tenant = settings.MSGRAPH_TENANT_ID
        self.client_id = settings.MSGRAPH_CLIENT_ID
        self.secret = settings.MSGRAPH_CLIENT_SECRET
        self.sender = settings.MSGRAPH_SENDER
        missing = [name for name, value in (
            ("MSGRAPH_TENANT_ID", self.tenant), ("MSGRAPH_CLIENT_ID", self.client_id),
            ("MSGRAPH_CLIENT_SECRET", self.secret), ("MSGRAPH_SENDER", self.sender)) if not value]
        if missing:
            raise ImproperlyConfigured(f"Microsoft Graph email needs {', '.join(missing)} in .env.")

    # -- sign-in ------------------------------------------------------------

    def _access_token(self, renew=False):
        with _token_lock:
            if not renew and _token["value"] and time.time() < _token["expires"] - EARLY:
                return _token["value"]
            form = urlencode({
                "client_id": self.client_id, "client_secret": self.secret,
                "scope": SCOPE, "grant_type": "client_credentials",
            }).encode()
            status, detail = _call(Request(TOKEN_URL.format(tenant=quote(self.tenant)), data=form, method="POST",
                                           headers={"Content-Type": "application/x-www-form-urlencoded"}))
            if status != 200 or "access_token" not in detail:
                raise GraphError(f"Microsoft sign-in failed ({status}): {_reason(detail)}")
            _token["value"] = detail["access_token"]
            _token["expires"] = time.time() + int(detail.get("expires_in", 3600))
            return _token["value"]

    # -- sending ------------------------------------------------------------

    @staticmethod
    def _payload(message):
        html = next((content for content, kind in getattr(message, "alternatives", []) if kind == "text/html"), None)
        people = lambda addresses: [{"emailAddress": {"address": parseaddr(a)[1]}} for a in addresses]  # noqa: E731
        body = {
            "subject": message.subject,
            "body": {"contentType": "HTML" if html else "Text", "content": html or message.body},
            "toRecipients": people(message.to),
        }
        if message.cc:
            body["ccRecipients"] = people(message.cc)
        if message.bcc:
            body["bccRecipients"] = people(message.bcc)
        if message.reply_to:
            body["replyTo"] = people(message.reply_to)
        return {"message": body, "saveToSentItems": False}

    def _send(self, message):
        data = json.dumps(self._payload(message)).encode()
        url = SEND_URL.format(sender=quote(self.sender))
        for attempt in (1, 2):
            request = Request(url, data=data, method="POST", headers={
                "Authorization": f"Bearer {self._access_token(renew=attempt == 2)}",
                "Content-Type": "application/json",
            })
            status, detail = _call(request)
            if status == 202:                  # Graph accepts the message for delivery
                return
            if status != 401 or attempt == 2:  # a 401 may be an expired token: renew once
                raise GraphError(f"Microsoft Graph refused the email ({status}): {_reason(detail)}")

    def send_messages(self, email_messages):
        sent = 0
        for message in email_messages:
            if not message.recipients():
                continue
            try:
                self._send(message)
                sent += 1
            except GraphError:
                if not self.fail_silently:
                    raise
        return sent
