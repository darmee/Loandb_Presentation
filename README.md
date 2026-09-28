# Loan Reports Portal

A read-only web view onto the loan application tables in `dash_loans`
(PostgreSQL), with filtering, Excel download and document access.

Sibling to the Payment Requests portal, and deliberately built the same way -
same auth hardening, same audit logging, same deployment shape. Where it
differs, it is because this database differs, and each of those places is
called out below.

## Read-only, three ways over

1. **Database grant.** The `loan_reader` role holds `SELECT` only - PostgreSQL
   itself rejects any write. **Do not connect as `postgres`**: with a superuser
   this first layer does not exist.
2. **Router.** `loans/routers.py` raises `ReadOnlyDatabaseError` on any write
   routed to the `loans` app, and blocks migrations against the `loans`
   connection so Django cannot create its own tables in the loan schema.
3. **Model.** Every model is `managed = False`. Django never creates, alters or
   drops these tables.

Django's own tables (users, sessions, admin log) live in `local.sqlite3`,
entirely separate from the loan database.

All three are asserted by the test suite, not just documented:

    .venv/bin/python manage.py test loans.tests.test_readonly

Creating the read-only role:

```sql
CREATE ROLE loan_reader LOGIN PASSWORD '...';
GRANT CONNECT ON DATABASE dash_loans TO loan_reader;
GRANT USAGE ON SCHEMA public TO loan_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO loan_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO loan_reader;
```

The last line matters: it covers tables added later, so a new loan product does
not silently become invisible to this portal.

## Three products, one workflow

| Table | Model | URL |
|---|---|---|
| `cashback_request` | `CashbackRequest` | `/cashback/` |
| `cash_for_car_request` | `CashForCarRequest` | `/cash-for-car/` |
| `public_sector_request` | `PublicSectorRequest` | `/public-sector/` |

They share an identical approval spine, so those columns live on the abstract
`LoanRequest`. They are **not** cleanly unionable, and the differences are
load-bearing:

- `cashback_request` calls the family name `surname`; the others use `last_name`
- `public_sector_request` has no `middle_name` at all
- `cash_for_car_request` carries both `gender` and `sex`, and both `account_no`
  (char 10) and `account_number` (varchar 20)

`/all/` therefore filters each table separately and merges in Python rather than
issuing a `UNION`. That is reasonable at roughly 700 rows; if these tables reach
the hundreds of thousands, replace it with a database view.

## There is no status column

An application's state is implied by five booleans. `loans/status.py` derives it
once, in two forms generated from a single list:

- `status_expression()` - a SQL `CASE`, so filtering, sorting and the dashboard
  totals happen in the database
- `derive_status()` - the same rules in Python, for a single object

If those two ever disagreed, the stat tiles would stop reconciling with the rows
beneath them and it would look like a database fault. `test_status.py` proves
they agree across all 64 combinations of the flags.

**The order of `STATUS_RULES` is the business rule.** Currently:

    rejected > disbursed > correction_requested > control_approved
             > credit_approved > reviewed > pending

An open correction request outranks the approvals because it is what actually
blocks the application. If that is wrong, reorder that one list - everything
else follows.

## Timezones

Every timestamp in `dash_loans` is `timestamptz`, so `USE_TZ = True` - unlike
the Payment Requests portal, which reads naive datetimes and sets it `False`.
Getting this backwards shifts every displayed time by an hour.

Excel cannot store an aware datetime at all, so exports convert to Africa/Lagos
and then drop the tzinfo (`_naive_local` in `loans/views.py`). Writing the raw
UTC value would put every timestamp in every report exactly one hour behind the
portal.

## Documents

`request_document` stores file content inline as base64 in a `data` column,
across 5,000+ rows. Three consequences, all handled:

- `RequestDocument.objects` **defers `data` on every query**. Loading it
  requires `with_content()`. A list view that selected it by accident would pull
  every uploaded ID scan into memory.
- Documents are fetched **through their parent application**, never by id alone,
  so row visibility governs documents automatically - including any rule added
  later.
- `mime_type` is whatever the upload set, so only a short whitelist is served
  inline. Everything else downloads as `application/octet-stream`. An uploaded
  HTML file served inline would execute in the viewer's session, with a live
  session cookie beside it.

Every document access is written to the audit log.

## Local development

The loan database listens on loopback on the Windows server. Two options.

**Fabricated local copy** (no tunnel needed):

    USE_SAMPLE_DB=True .venv/bin/python manage.py seed_sample_db
    USE_SAMPLE_DB=True .venv/bin/python manage.py runserver

This creates `dash_loans_dev` on your local PostgreSQL and fills it with
invented people. PostgreSQL rather than SQLite deliberately: this application
depends on timezone-aware timestamps, `numeric` precision and `CASE`/`WHEN`
ordering, none of which SQLite reproduces faithfully.

The command refuses to run unless `USE_SAMPLE_DB=True`, so it cannot reach the
real database.

**The real database, through a tunnel:**

    ssh -N -L 5433:127.0.0.1:5432 Alatiseo@132.145.47.17

then set `LOANS_DB_HOST=127.0.0.1` and `LOANS_DB_PORT=5433` in `.env`.

Without a route the app shows a readable connectivity page (HTTP 503) rather
than a stack trace.

## Verifying against the real database

`seed_sample_db` builds the development schema **from the models**, so it proves
the application agrees with the models - never that the models agree with the
real table. Two scripts close that gap, and both should run after any upstream
schema change:

    .venv/bin/python scripts/verify_schema.py
    .venv/bin/python scripts/verify_request_types.py

`verify_schema.py` compares every mapped column against `information_schema` and
reports both directions - columns the model has that the table does not, and
columns the table has that the model has missed.

`verify_request_types.py` checks the strings in `REQUEST_TYPE_BY_MODEL`.
**Run this one first on the live database.** The supporting tables point at
applications through a `(request_type, request_id)` pair rather than a foreign
key, and if those strings are wrong nothing raises - documents and history just
come back empty on every application, which looks like "no attachments" rather
than like a bug.

This is not hypothetical. The first run against production found two of the
three wrong: the real values are **`cfc`** and **`psl`**, not `cash_for_car`
and `public_sector`. Had it gone live unchecked, 1,088 documents across two
products would have been invisible with no error anywhere. The values are now
pinned by `loans/tests/test_schema.py` so changing one requires deleting a
test.

The live data also carries a fourth value, `extension`, belonging to
`tenor_extension` - see below.

## Exports and PII

The tables hold BVN, NIN, dates of birth, mothers' maiden names, addresses,
next-of-kin details and account numbers. `EXPORT_INCLUDE_PII` is read from the
environment and **defaults to `False`**, masking those columns in exports and on
detail pages. Unmasking is a deployment decision, not a code edit.

This is the opposite default from the Payment Requests portal, where it is
hardcoded `True`. BVN and NIN are regulated identifiers; masking by default is
the safer failure.

Exports always cover every row matching the current filters, not just the
visible page, and every export is recorded: who, when, which filters, how many
rows, whether PII was included.

## Authentication

Django's own auth (PBKDF2-SHA256, CSRF on every form, signed session cookies),
plus:

- **Lockout** - 5 failed attempts locks that username+IP pair for 30 minutes
  (`django-axes`). Pairing on username+IP rather than username alone means an
  attacker cannot lock a real user out of their own account from elsewhere.
- **Sessions** - expire after 8 hours, and on browser close. Sliding window.
- **Cookies** - HttpOnly, SameSite=Lax; `Secure` plus HSTS and an HTTPS redirect
  switch on automatically whenever `DEBUG` is off.
- **Audit log** - `logs/audit.log` records every export, every document access
  and every failed login. Rotates at 5MB.

Row visibility is currently "any authenticated user sees everything", but every
query goes through `LoanRequestQuerySet.visible_to(user)`. Per-user rules are
expected; that method is the single place they go, and it already denies by
default so a half-configured account shows nothing rather than everything.

Not included, and worth deciding on: **two-factor authentication**. For an
internal tool on a private network that may be acceptable; if this is ever
reachable from the internet, add `django-otp` before exposing it.

## Deployment

Same shape as the Payment Requests portal: Waitress under NSSM, behind Caddy for
TLS. See that project's README for the NSSM and Caddy service definitions - the
only differences here are the application directory and the port.

    py -m venv .venv
    .venv\Scripts\pip install -r requirements.txt
    copy .env.example .env      # then edit it

Then:

    .venv\Scripts\python manage.py collectstatic --noinput
    .venv\Scripts\python manage.py migrate
    .venv\Scripts\python manage.py createsuperuser
    .venv\Scripts\python scripts\verify_request_types.py
    .venv\Scripts\python scripts\verify_schema.py
    .venv\Scripts\python serve.py

Generate a secret key with:

    .venv\Scripts\python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"

**Port.** 8000-8002 are taken by other applications and 8500 by the Payment
Requests portal. This defaults to 8600. Check before choosing:

    Get-NetTCPConnection -State Listen | Select-Object -ExpandProperty LocalPort | Sort-Object -Unique

Behind Caddy, set `DJANGO_BIND_HOST=127.0.0.1` so Waitress answers only on
loopback - left on `0.0.0.0`, the port stays reachable directly and anyone who
knows it bypasses TLS entirely.

### Health check

`GET /healthz/` needs no login and returns:

    {"app": "ok", "database": "ok"}          200
    {"app": "ok", "database": "unreachable"} 503

It reports whether the loan database answers, never what is in it and never the
connection error.

## What is and is not covered

Every column of every table the portal reports on is mapped - 284 of 286, with
the two exceptions deliberate and asserted by `loans/tests/test_schema.py`:

| Table | Columns | Mapped |
|---|---|---|
| `cashback_request` | 66 | 66 |
| `cash_for_car_request` | 120 | 120 |
| `public_sector_request` | 64 | 64 |
| `admins` | 11 | 9 |
| `request_document` | 9 | 9 |
| `approval_audit_log` | 9 | 9 |
| `loan_comment` | 7 | 7 |

`admins.password_hash` is not mapped because nothing in a reporting portal
should be able to read it, and an unmapped column cannot leak through a
careless `values()` or a detail template. `admins.roles` is not mapped because
this portal does not delegate authorisation to the origination system.

Eleven further tables in `dash_loans` are not modelled: lookups (`lga`,
`setting`, `referral_code`, `floauto_outlet`), transient auth state
(`phone_otp`, `idempotency_key`), superseded pre-migration tables (`legacy_*`),
and `payroll_record`. The reasons are recorded in
`loans/tests/schema_snapshot.py`.

**`tenor_extension` is the one worth revisiting.** It is effectively a fourth
request type - it has its own `reference_no`, `status` and review columns - and
it is not in the portal. The row-count estimate said zero, but the live
`request_document` table already carries one row tagged `extension`, so it is
in use. If tenor extensions matter for reporting, they need a product tab of
their own.

## Security posture

Verified by `loans/tests/test_access.py` and `manage.py check --deploy`, which
reports no issues with `DEBUG=False`.

- **No JavaScript at all.** The app ships no scripts, inline handlers or
  bundles, so the Content-Security-Policy sets `script-src 'none'` rather than
  the usual compromise. A template escaping bug cannot become code execution.
- **Served documents are additionally sandboxed** with `Content-Security-Policy:
  sandbox`, and anything outside a short mime whitelist downloads as
  `application/octet-stream`.
- **Uploaded filenames are sanitised before reaching a header.** `file_name` is
  attacker-controlled; a double quote in it would otherwise close the
  `Content-Disposition` value early and inject further parameters.
- Headers on every response: `X-Frame-Options: DENY`, `X-Content-Type-Options:
  nosniff`, `Referrer-Policy: same-origin`, `Permissions-Policy` denying camera,
  microphone, geolocation, payment and USB, and `X-Robots-Tag`.
- Documents resolve through their parent application, so a document id alone
  grants nothing.

### Known gaps, in order of importance

1. **No two-factor authentication.** Acceptable on a private network; add
   `django-otp` before this is reachable from the internet.
2. **`/admin/` is exposed.** It is how users get created, and `django-axes`
   rate-limits it, but it is a known URL on a public-facing app. Consider
   restricting it by IP at the reverse proxy.
3. **Document downloads are not rate-limited.** An authenticated user could
   enumerate and pull every uploaded file. The audit log would show exactly who
   did, but nothing would stop them.
4. **Documents are decoded fully into memory.** Fine for the current file sizes;
   a very large upload would be a memory spike per request.
5. **`EXPORT_INCLUDE_PII=True` is a single environment variable** away from
   putting BVNs into a spreadsheet. That is the intended control, but it is one
   line in `.env` with no second check.

## Tests

    .venv/bin/python manage.py test loans

36 tests covering the read-only guarantee, the status derivation across all flag
combinations, PII masking, document access control and the export format. The
loan tables are unmanaged, so `loans/tests/base.py` builds them from the model
definitions and truncates them between tests - Django's own flush only touches
tables it manages.

## Search engines

Marked "do not index" three ways: a `<meta name="robots">` on every page, an
`X-Robots-Tag` header on **every** response (which is what covers the Excel
downloads and served documents, and the static files WhiteNoise returns before
the rest of the middleware runs), and `/robots.txt` returning `Disallow: /`.

These are requests, not access control. Every page already requires a login;
what this prevents is the sign-in page turning up in search results.
