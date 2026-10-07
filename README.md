# Loan Journey Presentation

A read-only, single-page presentation of the loan application pipeline in
`dash_loans` (PostgreSQL). Signing in goes straight to it - there is no
dashboard, list or detail page any more.

## The presentation

One page at `/`, in the shape of the original deck, dark by default (with a
light/dark switch remembered per browser). On any computer screen - including
short laptop screens with Windows display scaling - **every page is exactly one
screen and nothing scrolls**; only a phone-width window (under 760px) becomes
a scrolling page. The play button advances through the deck every 15 seconds;
data refreshes itself every 10 minutes.

The period buttons (All time - the default - 12 months, 90 days, 30 days, This
month, or any custom dates) apply to every page.

| Page | What it shows (deck pages: four charts, two on top, two below) |
|---|---|
| Overview | **All requests**: status tiles; request type comparison (pie); final outcome (pie) with a **See why...** dropdown; loan volume by product (bar - hover ranks the products and stars the highest); applications per month (per day when the period is about three months or shorter - This month, 30 days, 90 days) |
| Monthly overview | Applications (or value) per month, loan size distribution, top states, officer performance |
| Cashback / Public Sector | Final outcome with **See why...**, applications per month (per day for short periods; click a day for its requests), loan size distribution, top states |
| Cash for Car | Final outcome, applications per month, loan size distribution / top states (a switch on one card), and the **Dash vs Floauto loan split** with who took the higher share per loan |
| Daily activity | Submitted / reviewed / approved / disbursed / rejected per day (14 days on screen, slider to go back), a day-by-day table and a calendar |
| Applications vs disbursements | Running totals, value requested vs disbursed |
| Compare periods | Two date ranges side by side |
| Top insights | Plain-language findings, last in the deck |

**See why...** dropdown; loan volume by product (bar - hover ranks the products and stars the highest); applications per month |
| Monthly overview | Applications per month, values requested per month, loan size distribution, top states, officer performance |
| Cashback / Cash for Car / Public Sector | The same tiles, final outcome with **See why...**, applications per month, loan size distribution and top states, for one product. Cash for Car adds the **Dash vs Floauto loan split** and how many loans each took the higher share on |
| Daily activity | Submitted / reviewed / approved / disbursed / rejected per day (14 days on screen, slider to go back), a day-by-day table and a calendar |
| Applications vs disbursements | Daily applications against disbursements, running totals, value requested vs disbursed |
| Loan journey | Search any request and follow it step by step |
| Compare periods | Two date ranges side by side |
| Top insights | Plain-language findings, last in the deck |

**See why...** - every status tile, pie slice, legend row and the dropdown
open the list of requests behind them, topped by a breakdown of *why*: the
rejection reasons for rejected requests, the step each in-progress request is
waiting at, the correction asked for. Click a reason to filter the list; click
a request to see its journey (step by step, who moved it and when) in the
same panel, with a back arrow to the list. Clicking a day on any daily chart opens that
day's details (e.g. "Tue 22 Sep 2026 - Submitted 27, Reviewed 13, Approved 9,
Disbursed 8, Rejected 2").

### Two ways of counting

* **Activity** - each milestone counted on the day it happened, whenever the
  application was submitted. The daily charts, KPI tiles and period
  comparison counts use this. "15 Sep: Reviewed 31" means 31 applications
  were reviewed that day.
* **Cohort** - applications *submitted* in the window, followed to wherever
  they are now. Funnels, conversion rates, drop-off and the completion rate use
  this, because a rate is only meaningful when the numerator is a subset of the
  denominator. An older period's cohort has had longer to finish, so a lower
  completion rate for the current period is expected; the insight text says so.

### Definitions

* **Gates** are the work between two milestones: Review (Submitted -> Reviewed),
  Credit approval, Internal control, Disbursement.
* A rejection is placed at the gate after the furthest milestone the
  application reached - the one it could not pass. This keeps the rejection
  breakdown consistent with the funnel; the free-text `rejected_stage` is only
  used when every milestone flag is set.
* **Stuck** means waiting at a gate for 7 days or more (`STALLED_DAYS` in
  `loans/journey.py`). **Bottleneck** is the gate with the most team-side work
  weighted by how long it has waited; **Slowest** is the gate with the longest
  median time to clear; **Biggest drop-off** loses the most applications to
  rejection or being stuck.
* Rejection reasons and correction messages are free text. Exact strings are
  grouped ignoring case and punctuation, and also folded into categories by
  the keyword lists `REJECTION_CATEGORIES` / `CORRECTION_CATEGORIES` in
  `loans/journey.py`. Extend those as real reasons appear - anything
  unmatched shows as "Other".
* Days are Africa/Lagos days.

### How it is built

* `loans/journey.py` - every analysis, as plain Python over one list of
  `Record`s loaded per request (one query per product). Unit-tested without a
  database in `loans/tests/test_journey.py`.
* `loans/views.py` - the page and the JSON endpoints under `/api/`.
* `templates/loans/presentation.html`, `static/js/presentation.js`,
  `static/css/app.css` - the page. Charts are ECharts 5.6, vendored into
  `static/vendor/echarts/` so nothing loads from a CDN.

Loading every application per request is fine at the current few thousand
rows (the summary endpoint answers in about half a second over 2,000). If the
tables reach the hundreds of thousands, move the daily aggregation into SQL
first; it is the only part that touches every row.

### If the presentation shows no data

    python manage.py presentation_status

prints which database the app is reading (sample or live, name, host), how
many applications each product has, their date range, and whether anything
falls in the default 30-day window. Then:

* **Data exists but is older than 30 days** (common with a sample database
  seeded a while ago): the page jumps to the latest 30 days of data on first
  load and says so. To get fresh, recent sample data instead, re-run
  `python manage.py seed_sample_db` - it rebuilds `dash_loans_dev` only.
* **A red "script has not started" box** means the browser did not run
  `presentation.js` - press Ctrl+F5. On a production server (`DEBUG=False`),
  run `manage.py collectstatic` after every update.
* **"The loan database is not reachable"** - PostgreSQL or the SSH tunnel is
  down, or the `SAMPLE_DB_*` / `LOANS_DB_*` settings in `.env` are wrong.

Script and stylesheet URLs carry a `?v=` version taken from the files'
modification times, so a browser cannot keep running an old copy after an
update. `config/settings.py` also pins `.js`/`.css` content types, because
Windows can register `.js` as `text/plain`, which browsers refuse to execute.

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

| Table | Model | Product filter value |
|---|---|---|
| `cashback_request` | `CashbackRequest` | `cashback` |
| `cash_for_car_request` | `CashForCarRequest` | `cash-for-car` |
| `public_sector_request` | `PublicSectorRequest` | `public-sector` |

They share an identical approval spine, so those columns live on the abstract
`LoanRequest`. They are **not** cleanly unionable, and the differences are
load-bearing:

- `cashback_request` calls the family name `surname`; the others use `last_name`
- `public_sector_request` has no `middle_name` at all
- `cash_for_car_request` carries both `gender` and `sex`, and both `account_no`
  (char 10) and `account_number` (varchar 20)

The presentation therefore loads each table separately and merges in Python
rather than issuing a `UNION`.

## There is no status column

An application's state is implied by five booleans. `loans/status.py` derives it
once, in two forms generated from a single list:

- `status_expression()` - a SQL `CASE`, so the status is computed by the database
- `derive_status()` - the same rules in Python, for a single object

If those two ever disagreed, the presentation's counts would stop reconciling
with an individual application's journey and it would look like a data fault. `test_status.py` proves
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

The presentation buckets every event into an Africa/Lagos day before counting
it (`journey.local_date`), so an application submitted at 00:30 Lagos time
counts on that day, not the previous UTC one. `test_journey.py` pins this.

## Local development

The loan database listens on loopback on the Windows server. Two options.

**Fabricated local copy** (no tunnel needed):

    USE_SAMPLE_DB=True .venv/bin/python manage.py seed_sample_db
    USE_SAMPLE_DB=True .venv/bin/python manage.py runserver

This creates `dash_loans_dev` on your local PostgreSQL and fills it with about
2,000 invented applications, weighted towards recent weeks, with rejections at
every gate, corrections that are and are not resolved, and an audit trail that
matches each application's path - enough for every tab to have something to
show. PostgreSQL rather than SQLite deliberately: this application
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

A third checks the presentation's own figures - daily activity, funnel and
outcome counts - against independent raw SQL for a window of N days:

    .venv/bin/python scripts/verify_journey.py 30

`verify_schema.py` compares every mapped column against `information_schema` and
reports both directions - columns the model has that the table does not, and
columns the table has that the model has missed.

`verify_request_types.py` checks the strings in `REQUEST_TYPE_BY_MODEL`.
**Run this one first on the live database.** The supporting tables point at
applications through a `(request_type, request_id)` pair rather than a foreign
key, and if those strings are wrong nothing raises - the audit trail and comments
on the Loan journey tab just come back empty on every application, which looks
like "no history" rather than like a bug.

This is not hypothetical. The first run against production found two of the
three wrong: the real values are **`cfc`** and **`psl`**, not `cash_for_car`
and `public_sector`. Had it gone live unchecked, the history of two
products would have been invisible with no error anywhere. The values are now
pinned by `loans/tests/test_schema.py` so changing one requires deleting a
test.

The live data also carries a fourth value, `extension`, belonging to
`tenor_extension` - see below.

## Authentication

Django's own auth (PBKDF2-SHA256, CSRF on every form, signed session cookies),
plus:

- **Lockout** - 5 failed attempts locks that username+IP pair for 30 minutes
  (`django-axes`). Pairing on username+IP rather than username alone means an
  attacker cannot lock a real user out of their own account from elsewhere.
- **Sessions** - expire after 8 hours, and on browser close. Sliding window.
- **Cookies** - HttpOnly, SameSite=Lax; `Secure` plus HSTS and an HTTPS redirect
  switch on automatically whenever `DEBUG` is off.
- **Audit log** - `logs/audit.log` records every individual application opened
  on the Loan journey tab (who, which application, from where) and every failed
  login. Rotates at 5MB.

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

- **Scripts only from the app's own static files.** The Content-Security-Policy
  is `script-src 'self'` with no `'unsafe-inline'` and no `'unsafe-eval'`. The
  only inline `<script>` is Django's `json_script` data block, which browsers
  never execute; a test fails if any other inline script appears. The chart
  library is vendored rather than loaded from a CDN for the same reason.
- **Server data is never inserted as HTML.** The page builds its DOM with
  `textContent`; chart tooltips, the one place HTML strings are used, escape
  every value first.
- **The API answers 401, never a login redirect,** so a session that expires
  mid-presentation sends the viewer to sign in cleanly.
- **No identity numbers leave the server.** The journey and drill-down
  endpoints return name, reference, product, amount, officer and state - never
  BVN, NIN, date of birth, phone, email, address or account numbers.
- Headers on every response: `X-Frame-Options: DENY` and `frame-ancestors
  'none'` (the presentation no longer uses iframes), `X-Content-Type-Options:
  nosniff`, `Referrer-Policy: same-origin`, `Permissions-Policy` denying camera,
  microphone, geolocation, payment and USB, and `X-Robots-Tag`.

### Known gaps, in order of importance

1. **No two-factor authentication.** Acceptable on a private network; add
   `django-otp` before this is reachable from the internet.
2. **`/admin/` is exposed.** It is how users get created, and `django-axes`
   rate-limits it, but it is a known URL on a public-facing app. Consider
   restricting it by IP at the reverse proxy.
3. **Applicant names are visible to every signed-in user** in drill-downs and the
   journey search. That matches the old portal, and journey views are audited,
   but if presentation viewers should not see names, mask them in
   `Record.summary()` and `api_journey`.

## Tests

    .venv/bin/python manage.py test loans

59 tests covering the read-only guarantee, the status derivation across all 64
flag combinations, sign-in and API access control, the security headers, and
the journey analyses (funnel monotonicity, every lost applicant accounted for
at a gate, Lagos-day bucketing, reason grouping, drill-down lists matching the
chart numbers). The loan tables are unmanaged, so `loans/tests/base.py` builds
them from the model definitions and truncates them between tests - Django's own
flush only touches tables it manages.

## Search engines

Marked "do not index" three ways: a `<meta name="robots">` on every page, an
`X-Robots-Tag` header on **every** response (which is what covers the JSON
endpoints and the static files WhiteNoise returns before the rest of the
middleware runs), and `/robots.txt` returning `Disallow: /`.

These are requests, not access control. Every page already requires a login;
what this prevents is the sign-in page turning up in search results.
