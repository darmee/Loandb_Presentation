# Forgot password with Microsoft Graph: a step-by-step guide

How the dashboard's "Forgot password?" feature was built, what each part does
and why, and how to rebuild, change, test and deploy it yourself. Every file
named here is in this repository.

---

## 1. What it does

1. On the sign-in page, the user clicks **Forgot password?**
2. They type their work email. Only `@dash-mfb.com` addresses are accepted.
3. The page always says **"Check your email"**, whether or not that address
   has an account, so nobody can use the page to find out who has one.
4. If the address belongs to an account, Django creates a **one-time link**
   that expires after **1 hour**. The dashboard sends it from a Microsoft 365
   mailbox through **Microsoft Graph**.
5. The user opens the link, types a new password twice, and sees
   **"Password changed"**.
6. If the account was locked for too many wrong passwords, the lockout is
   lifted.

### How the pieces fit

```
login.html  --"Forgot password?"-->  /password-reset/             ResetRequestView     (loans/password_reset.py)
                                          | checks the domain, the hourly limit
                                          | builds the one-time link
                                          v
                                    Django email system ---> EMAIL_BACKEND (chosen in config/settings.py)
                                                               |-- Graph   -> loans/mail_graph.py -> Microsoft
                                                               |-- SMTP    -> Django's built-in SMTP sender
                                                               '-- neither -> printed in the terminal
                                    /password-reset/sent/          "Check your email"
email link ---------------------->  /password-reset/<uid>/<token>/ ResetConfirmView     (set a new password)
                                    /password-reset/done/          "Password changed"
```

**The main idea:** Django already has a complete, secure password reset (the
token, the expiry, the pages). We reused it and added only three things: our
rules (company domain, rate limit, lockout), our page design, and a way to
send the email through Microsoft Graph.

---

## 2. The files

| File | New or changed | What it is |
|---|---|---|
| `loans/password_reset.py` | new | Our rules on top of Django's reset: domain check, rate limit, link address, lockout reset, logging |
| `loans/mail_graph.py` | new | Sends any Django email through Microsoft Graph |
| `config/settings.py` | changed | The reset settings, and the choice of email provider (Graph, SMTP or terminal) |
| `config/urls.py` | changed | The four `/password-reset/...` web addresses |
| `templates/registration/auth_card.html` | new | Shared layout for the reset pages: the sign-in card on its own |
| `templates/registration/password_reset_form.html` | new | Page 1: "Forgot your password?" (enter email) |
| `templates/registration/password_reset_done.html` | new | Page 2: "Check your email" |
| `templates/registration/password_reset_confirm.html` | new | Page 3: "Set a new password" (or "This link no longer works") |
| `templates/registration/password_reset_complete.html` | new | Page 4: "Password changed" |
| `templates/registration/password_reset_email.txt` | new | The email's text |
| `templates/registration/password_reset_subject.txt` | new | The email's subject line |
| `templates/registration/login.html` | changed | The "Forgot password?" link |
| `assets/css/app.css` | changed | Styles for the link and the reset pages (then rebuilt into `static/css/app.css`) |
| `.env.example` | changed | Lists every setting, with notes (no real values) |
| `.env` | **yours, never committed** | The real values: the Microsoft IDs and secret |
| `loans/tests/test_password_reset.py` | new | Tests for the whole flow |
| `loans/tests/test_mail_graph.py` | new | Tests for the Graph sender, against a fake Microsoft |

> **The file names in `templates/registration/` matter.** Django looks for
> exactly these names. Rename one and Django falls back to its own unstyled
> admin page.

---

## 3. Step by step

### Step 1: Settings (`config/settings.py`)

Add this block after `LOGOUT_REDIRECT_URL = "login"`:

```python
# --- forgot password -----------------------------------------------------------
# A reset link goes only to addresses on this domain, and works once for an hour.
PASSWORD_RESET_EMAIL_DOMAIN = os.getenv("PASSWORD_RESET_EMAIL_DOMAIN", "dash-mfb.com").strip().lstrip("@").lower()
PASSWORD_RESET_TIMEOUT = 60 * 60
# The address people open the dashboard at, e.g. https://loans.dash-mfb.com -
# the reset link in the email is built on it.
PUBLIC_URL = os.getenv("DJANGO_PUBLIC_URL", "").strip().rstrip("/")

def _graph(name):
    # MSGRAPH_TENANT_ID or MS_GRAPH_TENANT_ID - either spelling in .env.
    return (os.getenv(f"MSGRAPH_{name}") or os.getenv(f"MS_GRAPH_{name}") or "").strip()

MSGRAPH_TENANT_ID = _graph("TENANT_ID")
MSGRAPH_CLIENT_ID = _graph("CLIENT_ID")
MSGRAPH_CLIENT_SECRET = _graph("CLIENT_SECRET")
MSGRAPH_SENDER = _graph("SENDER")
EMAIL_HOST = os.getenv("EMAIL_HOST", "")
if MSGRAPH_TENANT_ID:
    EMAIL_PROVIDER = "graph"
    EMAIL_BACKEND = "loans.mail_graph.GraphEmailBackend"
elif EMAIL_HOST:
    EMAIL_PROVIDER = "smtp"
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
    EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
    EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "True").lower() == "true"
    EMAIL_USE_SSL = os.getenv("EMAIL_USE_SSL", "False").lower() == "true"
    EMAIL_TIMEOUT = 15
else:
    EMAIL_PROVIDER = ""
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = (os.getenv("DEFAULT_FROM_EMAIL", "") or MSGRAPH_SENDER
                      or os.getenv("EMAIL_HOST_USER", "") or "no-reply@dash-mfb.com")
```

What each part means:

| Setting | Meaning |
|---|---|
| `os.getenv("NAME", default)` | Reads `NAME` from `.env`; uses `default` if it isn't there. The `load_dotenv(...)` call at the top of `settings.py` loads `.env`. **Secrets are never typed in this file.** |
| `PASSWORD_RESET_EMAIL_DOMAIN` | The only email domain allowed. Change it in `.env` without touching code. |
| `PASSWORD_RESET_TIMEOUT` | How long a link works, **in seconds**. `60 * 60` is 1 hour. Django's own setting name. |
| `PUBLIC_URL` | The real address of the dashboard. The link in the email is built on it. On the server Waitress sits behind IIS and only sees `127.0.0.1:8600`, so without this setting the email link would point there and not work for anyone else. |
| `_graph(name)` | Reads either `MSGRAPH_X` or `MS_GRAPH_X`. This was added after the `.env` used `MS_GRAPH_...` and nothing was found. |
| `EMAIL_BACKEND` | **The key setting.** Django sends every email through the class named here. Graph is used if the tenant ID is set, then SMTP if `EMAIL_HOST` is set, otherwise the **console** backend, which prints the email in the terminal instead of sending it. |
| `EMAIL_PROVIDER` | Our own label ("graph", "smtp" or ""). The reset view checks it to warn in the log when nothing is actually being sent. |
| `DEFAULT_FROM_EMAIL` | The "From" shown on the email. |

Also add this to `LOGGING["loggers"]` in the same file, so a failed send is
written to `logs/audit.log`:

```python
"django.contrib.auth": {"handlers": ["audit_file", "console"], "level": "WARNING", "propagate": False},
```

### Step 2: Our rules (`loans/password_reset.py`)

```python
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
            audit.warning("password reset email NOT SENT: no email provider in .env ...")
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
```

What each part does:

- **Subclassing (`class X(auth_forms.PasswordResetForm)`).** We take Django's
  ready-made form and view and change only what we need. Everything else,
  including the secure token, comes from Django.
- **`clean_email`.** Django calls any method named `clean_<field>` to check
  that field. Raising `ValidationError` shows the message on the page and
  stops there.
- **`get_users`.** Django calls this to find the accounts to email. Ours also
  drops any account whose *saved* email isn't on the company domain, in case
  an admin typed a Gmail address into an account.
- **`_over_limit`.** Counts requests in Django's cache: 3 an hour per email,
  20 per computer (IP address). Over the limit, nothing is sent, but the page
  looks the same, so it gives nothing away.
- **`form.save(...)`.** This is where Django builds the link and sends the
  email. `domain_override` and `use_https` make the link use `PUBLIC_URL`.
- **`reset_lockout`.** From `django-axes`, which locks an account after 5 wrong
  passwords. A successful reset clears the lock.
- **`audit.info / audit.warning`.** These write to `logs/audit.log`, so you can
  see every request, every completion, and when an email wasn't really sent.
- **How the link is protected:** the link carries `uidb64`, the user's ID
  encoded, and a `token`, a signature made from the user's current password
  and the `DJANGO_SECRET_KEY`. Once the password changes, the token no longer
  matches, so a link works **once**. After `PASSWORD_RESET_TIMEOUT` it
  **expires**. Nothing is stored in the database for this.

### Step 3: Sending through Microsoft Graph (`loans/mail_graph.py`)

Django sends every email through an "email backend", the class named in
`EMAIL_BACKEND`. This file is a backend that hands the email to Microsoft
Graph instead of an SMTP server. It uses only Python's standard library, so
there is nothing to `pip install`.

It works in two calls to Microsoft:

**1. Sign in and get a token** (OAuth 2.0 "client credentials", meaning the app
signs in as itself, with no user involved):

```
POST https://login.microsoftonline.com/<TENANT_ID>/oauth2/v2.0/token
     client_id=<CLIENT_ID>&client_secret=<SECRET>
     &scope=https://graph.microsoft.com/.default&grant_type=client_credentials
-> {"access_token": "...", "expires_in": 3599}
```

**2. Send the email** with that token:

```
POST https://graph.microsoft.com/v1.0/users/<SENDER>/sendMail
     Authorization: Bearer <access_token>
     {"message": {"subject": "...", "body": {"contentType": "Text", "content": "..."},
                  "toRecipients": [{"emailAddress": {"address": "ada@dash-mfb.com"}}]},
      "saveToSentItems": false}
-> 202 Accepted   (Graph has taken it for delivery)
```

The key parts of the file:

```python
class GraphEmailBackend(BaseEmailBackend):
    def __init__(self, fail_silently=False, **kwargs):
        ...  # reads MSGRAPH_* from settings; names any that are missing

    def _access_token(self, renew=False):
        ...  # step 1; the token is kept in memory and reused until ~2 minutes before it expires

    @staticmethod
    def _payload(message):
        ...  # turns a Django email into Graph's JSON (subject, body, to/cc/bcc, reply-to)

    def _send(self, message):
        ...  # step 2; on a 401 (token expired) it gets a new token and tries once more

    def send_messages(self, email_messages):
        ...  # what Django calls; returns how many were sent
```

Read the full file for the details. Every part is commented. Errors raise
`GraphError` with **Microsoft's own reason** (e.g. `ErrorAccessDenied`) and
never include the secret.

**Why a backend and not a direct call in the reset code?** Because then
*everything* Django sends goes through Graph: the reset email, the
`sendtestemail` command, and any email added later. Switching provider is
just a change in `.env`.

### Step 4: The web addresses (`config/urls.py`)

```python
from loans.password_reset import ResetConfirmView, ResetRequestView

urlpatterns = [
    ...
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    # Forgot password: request a link, "check your email", set a new one, done.
    path("password-reset/", ResetRequestView.as_view(), name="password_reset"),
    path("password-reset/sent/", auth_views.PasswordResetDoneView.as_view(), name="password_reset_done"),
    path("password-reset/done/", auth_views.PasswordResetCompleteView.as_view(), name="password_reset_complete"),
    path("password-reset/<uidb64>/<token>/", ResetConfirmView.as_view(), name="password_reset_confirm"),
    path("", include("loans.urls")),
]
```

- **The `name=` values must be exactly these.** Django's reset code redirects
  using these names (e.g. after the form it goes to `password_reset_done`).
- **`<uidb64>/<token>`** are the two parts of the emailed link; Django checks them.
- The two middle pages need no code of ours, so they use Django's views
  directly.

### Step 5: The pages (`templates/registration/`)

All four pages extend one layout, `auth_card.html`: the sign-in card centred
on its own, with a "← Back to sign in" link. Each page fills in `{% block card %}`.

| Template | Shown when | What Django passes in |
|---|---|---|
| `password_reset_form.html` | `/password-reset/` | `form` (with `form.email.errors`), plus our `company_domain` |
| `password_reset_done.html` | after submitting | — |
| `password_reset_confirm.html` | the emailed link | `validlink` (True/False); `form` with `new_password1`, `new_password2` |
| `password_reset_complete.html` | after saving | — |
| `password_reset_email.txt` | the email body | `user`, `protocol`, `domain`, `uid`, `token`, plus our `site_name`, `hours` |
| `password_reset_subject.txt` | the subject | — (keep it to **one line**) |

The email's body (`password_reset_email.txt`):

```
{% autoescape off %}Hello {{ user.get_username }},

Someone asked to reset the password for your {{ site_name }} account. If it was you, set a new password here:

{{ protocol }}://{{ domain }}{% url 'password_reset_confirm' uidb64=uid token=token %}

The link works once and expires in {{ hours }} hour{{ hours|pluralize }}.

If you did not ask for this, ignore this email - your password stays as it is.

{{ site_name }}
{% endautoescape %}
```

`{% autoescape off %}` is there because this is plain text, not HTML. Without
it, a `&` would become `&amp;`. **To reword the email, edit this file.** No
code change is needed.

### Step 6: The link on the sign-in page

In `templates/registration/login.html`, the Password label is wrapped so the
link sits on the same line:

```html
<div class="field-row">
  <label class="field-label" for="id_password">Password</label>
  <a class="field-link" href="{% url 'password_reset' %}">Forgot password?</a>
</div>
```

`{% url 'password_reset' %}` looks the address up by its `name=` from
`urls.py`, so it still works if the address ever changes.

### Step 7: The styles

Add these to `assets/css/app.css`, after `.login-card .sub { ... }`:

```css
.field-row { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
.field-link { font-size: 12.5px; font-weight: 600; color: var(--accent); text-decoration: none; }
.field-link:hover { text-decoration: underline; text-underline-offset: 3px; }
.login.login-solo { grid-template-columns: minmax(0, 440px); justify-content: center; }
.auth-notes { display: grid; gap: 8px; padding-left: 18px; list-style: disc; color: var(--ink-2); font-size: 13.5px; }
.login-card .banner p + p { margin-top: 4px; }
```

Then **rebuild** the stylesheet, because the browser only loads `static/css/app.css`:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_css.ps1
```

**Never edit `static/css/app.css` by hand.** The build overwrites it.

---

## 4. Setting up Microsoft (Entra ID)

This is done once, in the Microsoft 365 admin side. You may need your M365
admin.

1. Go to **https://entra.microsoft.com → Applications → App registrations →
   New registration**.
   - Name: e.g. `Loan Dashboard mail`. Accounts: *this organizational
     directory only*. Redirect URI: leave empty. Click **Register**.
2. On the app's **Overview** page, copy:
   - **Directory (tenant) ID**, which goes in `MS_GRAPH_TENANT_ID`;
   - **Application (client) ID**, which goes in `MS_GRAPH_CLIENT_ID`.
3. Go to **API permissions → Add a permission → Microsoft Graph →
   Application permissions →** tick **Mail.Send → Add**. Then click
   **Grant admin consent for <company>**. The status must show a green tick.
   - It must be **Application**, not *Delegated*. Delegated permissions need
     a signed-in user, and the dashboard sends with no one signed in.
4. Go to **Certificates & secrets → Client secrets → New client secret**.
   Copy the **Value** (not the *Secret ID*) into `MS_GRAPH_CLIENT_SECRET`.
   - It's shown **only once**. If you lose it, make a new one.
   - **It expires** (6–24 months). **Note the date in your calendar.** When it
     expires, emails stop with `AADSTS7000222`: make a new secret, update
     `.env` on the server, and restart.
5. **Choose the sender mailbox**, e.g. `robot@dash-mfb.com`. It must be a real,
   licensed (or shared) mailbox. It goes in `MS_GRAPH_SENDER`.
6. **Restrict the app to that one mailbox (strongly recommended).** The
   application `Mail.Send` permission lets the app send as **anyone** in the
   company. Limit it in Exchange Online PowerShell:

   ```powershell
   Connect-ExchangeOnline
   New-ApplicationAccessPolicy -AppId <CLIENT_ID> -PolicyScopeGroupId <mail-enabled security group containing robot@dash-mfb.com> -AccessRight RestrictAccess -Description "Loan dashboard sends only as robot"
   Test-ApplicationAccessPolicy -Identity robot@dash-mfb.com -AppId <CLIENT_ID>   # should say: Granted
   ```
   The policy can take up to an hour to apply.

---

## 5. The `.env` file: where the provider goes

`.env` sits in the project folder, holds the real values, and is **never
committed**: `.gitignore` excludes it, and the repository is public. Your
**laptop** and the **server** each have their own `.env`. Pushing doesn't
copy it, so set both.

```ini
# --- Forgot password: Microsoft Graph ---
MS_GRAPH_TENANT_ID=<Directory (tenant) ID>
MS_GRAPH_CLIENT_ID=<Application (client) ID>
MS_GRAPH_CLIENT_SECRET=<the secret Value>
MS_GRAPH_SENDER=robot@dash-mfb.com
DEFAULT_FROM_EMAIL=DashMFB Loan Dashboard <robot@dash-mfb.com>

# Server only: the address people open the dashboard at (no slash at the end).
DJANGO_PUBLIC_URL=https://<dashboard address>

# Optional: change the allowed domain (default dash-mfb.com).
# PASSWORD_RESET_EMAIL_DOMAIN=dash-mfb.com
```

- `MS_GRAPH_` or `MSGRAPH_`: either spelling works.
- No quotes and no spaces around `=`.
- **Leave `DJANGO_PUBLIC_URL` out on the laptop.** The link then points at
  your laptop (`http://127.0.0.1:8000/...`), which is right for testing.
- To use SMTP instead of Graph, leave the `MS_GRAPH_` lines out and fill in
  `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` and
  `EMAIL_USE_TLS` (see `.env.example`).
- **`.env` is only read when the app starts.** After every change, restart:
  `runserver` (Ctrl+C, then start it again) on the laptop, or
  `nssm restart LoanDashboard` on the server.

Every user who should be able to reset needs **their email saved on their
account**: `/admin/` → *Users* → the user → *Email address* → Save.

---

## 6. Testing on your laptop

Which slashes to use depends on your terminal:
**Git Bash** uses `/` (`.venv/Scripts/python`), and **PowerShell** uses `\`
(`.venv\Scripts\python`). In Git Bash, `\` is swallowed, which gives
`.venvScriptspython: command not found`. If your prompt shows `(.venv)`,
plain `python` also works.

1. **Check the app sees Graph, and that sign-in works.** This sends nothing:
   ```bash
   python manage.py shell -c "from django.conf import settings as s; print(s.EMAIL_PROVIDER, s.MSGRAPH_SENDER); from loans.mail_graph import GraphEmailBackend; GraphEmailBackend()._access_token(); print('sign-in OK')"
   ```
   Expect `graph robot@dash-mfb.com` and then `sign-in OK`. If it prints an
   empty provider, the `.env` names are wrong or the file isn't saved.
2. **Send a real test email:**
   ```bash
   python manage.py sendtestemail damola.jayeoba@dash-mfb.com
   ```
   Check your inbox and junk folder. This is Django's own command and uses
   whatever `EMAIL_BACKEND` is set.
3. **Try the whole flow:** run `python manage.py runserver`, open
   `http://127.0.0.1:8000/login/`, and go through **Forgot password?** → enter
   your email → open the emailed link → set a new password → sign in. This
   changes the password only in your laptop's `local.sqlite3`.
4. **Run the tests:**
   ```bash
   python manage.py test loans.tests.test_password_reset loans.tests.test_mail_graph
   ```
   These never contact Microsoft. They use a fake one and Django's in-memory
   mailbox.

---

## 7. Deploying to the server

```powershell
# laptop
git add -A
git commit -m "Forgot password through Microsoft Graph"
git push

# server, in C:\sites\loandb_Presentation (PowerShell)
git pull
.venv\Scripts\python manage.py collectstatic --noinput   # the new CSS - forget this and pages break
#  -> add the MS_GRAPH_ lines and DJANGO_PUBLIC_URL to the server's .env
nssm restart LoanDashboard
.venv\Scripts\python manage.py sendtestemail damola.jayeoba@dash-mfb.com
```

Then press Ctrl+F5 in the browser to load the new CSS, and check that each
user's email is saved in `/admin/`.

---

## 8. When something goes wrong

**First, look at `logs/audit.log`.** Every request, completion, refusal and
failed send is written there.

| What you see | Cause | Fix |
|---|---|---|
| Link printed in the terminal, no email | No provider found: `EMAIL_PROVIDER` is empty | Check the `.env` names and that the file is saved, then **restart** |
| Log: `NOT SENT: no email provider` | Same as above | Same as above |
| `.venvScriptspython: command not found` | Backslashes in Git Bash | Use `/` or just `python` |
| `AADSTS7000215: Invalid client secret` | You copied the Secret **ID**, not the **Value** | Make a new secret, copy the Value |
| `AADSTS7000222` | The secret has expired | New secret → `.env` → restart |
| `AADSTS700016` / `AADSTS90002` | Wrong client ID / tenant ID | Copy them again from the app's Overview |
| `ErrorAccessDenied` (403) | Mail.Send isn't an *Application* permission, admin consent is missing, or the access policy doesn't include the sender | Fix in Entra ID / Exchange; wait up to an hour |
| `MailboxNotEnabledForRESTAPI` / `ResourceNotFound` | The sender isn't a real licensed mailbox | Use a licensed or shared mailbox |
| "Check your email" but nothing arrives, and the log has no error | No account has that email saved, or it isn't @dash-mfb.com, or the limit (3/hour) was hit | Save the email in `/admin/`; look for `too many requests` in the log |
| Link opens `127.0.0.1:8600` | `DJANGO_PUBLIC_URL` isn't set on the server | Set it, then restart |
| "This link no longer works" | Already used, older than an hour, or the password changed since | Request a new link |
| Reset pages unstyled / server error after a pull | `collectstatic` wasn't run | Run it, restart |
| "Use your @dash-mfb.com email address." | Another domain was typed | Use the work email |

---

## 9. Changing it later

| To change | Where |
|---|---|
| Allowed domain | `.env`: `PASSWORD_RESET_EMAIL_DOMAIN=...` |
| How long a link works | `PASSWORD_RESET_TIMEOUT` in `config/settings.py` (seconds) |
| Requests allowed per hour | `RESET_LIMITS` in `loans/password_reset.py` |
| The email's wording / subject | `templates/registration/password_reset_email.txt` / `_subject.txt` |
| The pages' wording | `templates/registration/password_reset_*.html` |
| Sender mailbox | `.env`: `MS_GRAPH_SENDER` (it must be allowed by the access policy) |
| Switch to SMTP | `.env`: remove the `MS_GRAPH_` lines, add the `EMAIL_` lines |
| Password rules (length etc.) | `AUTH_PASSWORD_VALIDATORS` in `config/settings.py` |

**To remove the feature:** delete the two `loans/` files and their tests, the
`password_reset_*` and `auth_card.html` templates, the four `path(...)` lines
and the import in `urls.py`, the settings block, the `field-row` link in
`login.html`, and the CSS lines. Then rebuild the CSS and run the tests.

---

## 10. Worth knowing

- **Never put the secret in code, the README or a commit.** Only in `.env`.
  If it ever leaks, delete it in Entra ID straight away and make a new one.
- **Django's reset is the safe part. Don't rewrite it.** The token, expiry
  and single use all come from Django and are well tested. Only add rules
  around it, as `password_reset.py` does.
- **Saying the same thing either way is deliberate.** Showing "no such
  account" would let anyone test which emails have accounts.
- **The hourly counter lives in memory.** It resets when the service restarts.
  That's fine with one Waitress process.
- **The console backend is a safety net.** In development nothing is emailed
  by accident. In production, the `NOT SENT` warning in the log tells you
  it's happening.
- **The Graph token is kept in memory** and renewed about 2 minutes before it
  expires, so Microsoft isn't asked for a new one on every email.
- **The same pattern works for any email feature.** Once `EMAIL_BACKEND`
  points at Graph, `django.core.mail.send_mail(...)` anywhere in the code
  sends through it.
