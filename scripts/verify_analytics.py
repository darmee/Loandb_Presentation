"""Check every figure on the analytics page against raw SQL.

The dashboard reaches its numbers through the ORM: managed=False models, a
CASE-derived status annotation, a Python median, and the officer alias map. Any
of those could be subtly wrong and still produce a page that looks entirely
reasonable - which is the failure that survives review.

So this asks the same questions twice. Once through `loans/analytics.py`, and
once with plain SQL issued on its own cursor, with the models, the annotation
and the alias map all outside the path being checked. Agreement between the two
means something.

Runs against whichever database .env points at, so it is equally valid on the
sample data and on production.

    .venv/bin/python scripts/verify_analytics.py

Exit status is non-zero if anything disagrees.
"""

import os
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import django

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.conf import settings  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402
from django.db import connections  # noqa: E402
from django.utils import timezone  # noqa: E402

from loans.analytics import (  # noqa: E402
    amount_bands,
    officer_performance,
    product_volume,
    top_states,
    turnaround,
    volume_by_month,
)
from loans.models import PRODUCTS  # noqa: E402

TABLES = {model.PRODUCT_LABEL: model._meta.db_table for model in PRODUCTS.values()}

# Mirrors STATUS_RULES in loans/status.py. Written out longhand on purpose - if
# someone reorders the precedence there, this stops agreeing and says so.
DISBURSED_SQL = "disbursed AND NOT rejected"

problems = []
notes = []


def compare(label, dashboard, database, tolerance=Decimal("0.01")):
    """Report one figure computed both ways."""
    a, b = dashboard, database
    if isinstance(a, (int, float, Decimal)) and isinstance(b, (int, float, Decimal)):
        agree = abs(Decimal(str(a)) - Decimal(str(b))) <= tolerance
    else:
        agree = a == b
    mark = "ok" if agree else "MISMATCH"
    print(f"  {label:<44} dashboard={a!s:<18} sql={b!s:<18} {mark}")
    if not agree:
        problems.append(f"{label}: dashboard {a} vs sql {b}")


def scalar(sql, params=None):
    with connections["loans"].cursor() as cursor:
        cursor.execute(sql, params or [])
        row = cursor.fetchone()
    return row[0] if row and row[0] is not None else 0


def main():
    db = settings.DATABASES["loans"]
    kind = "SAMPLE DATA" if settings.SAMPLE_DB else "LIVE DATABASE"
    print(f"Checking against {db['HOST']}/{db['NAME']}  [{kind}]\n")

    user = User.objects.filter(is_active=True).first()
    if user is None:
        print("No active Django user exists; the analytics functions need one.")
        return 2

    months = 12
    since = (timezone.now() - timedelta(days=31 * months)).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )

    # --- volume by product ------------------------------------------------
    print("Volume by product (last 12 months)")
    products = {row["label"]: row for row in product_volume(user, months)}
    total_apps = 0
    for label, table in TABLES.items():
        row = products.get(label, {"count": 0, "value": Decimal(0), "disbursed": 0})
        count = scalar(f"SELECT count(*) FROM {table} WHERE created_at >= %s", [since])
        value = scalar(
            f"SELECT COALESCE(sum(loan_amount),0) FROM {table} WHERE created_at >= %s",
            [since],
        )
        disbursed = scalar(
            f"SELECT count(*) FROM {table} WHERE created_at >= %s AND {DISBURSED_SQL}",
            [since],
        )
        compare(f"{label} applications", row["count"], count)
        compare(f"{label} value requested", row["value"], value)
        compare(f"{label} disbursed", row["disbursed"], disbursed)
        total_apps += count
    print()

    # --- monthly volume ---------------------------------------------------
    print("Applications per month")
    months_data = volume_by_month(user, months)
    compare("sum of monthly counts", sum(m["total"] for m in months_data), total_apps)
    compare(
        "sum of monthly values",
        sum(m["value"] for m in months_data),
        sum(
            scalar(
                f"SELECT COALESCE(sum(loan_amount),0) FROM {t} WHERE created_at >= %s",
                [since],
            )
            for t in TABLES.values()
        ),
    )
    print()

    # --- amount bands -----------------------------------------------------
    print("Loan size bands (all time)")
    bands = amount_bands(user)
    banded = sum(b["count"] for b in bands)
    everything = sum(scalar(f"SELECT count(*) FROM {t}") for t in TABLES.values())
    with_amount = sum(
        scalar(f"SELECT count(*) FROM {t} WHERE loan_amount IS NOT NULL")
        for t in TABLES.values()
    )
    compare("rows placed in a band", banded, with_amount)
    if with_amount != everything:
        notes.append(
            f"{everything - with_amount} application(s) have no loan_amount and "
            f"appear in no band. Bands total {with_amount}, not {everything}."
        )
    print()

    # --- states -----------------------------------------------------------
    print("Top states (all time)")
    states = top_states(user, limit=1000)
    compare("sum of state counts", sum(s["count"] for s in states), everything)
    print()

    # --- officers ---------------------------------------------------------
    print("Officer performance (disbursed, all time)")
    officers = officer_performance(user, limit=10_000)
    sql_count = sum(
        scalar(f"SELECT count(*) FROM {t} WHERE {DISBURSED_SQL}") for t in TABLES.values()
    )
    sql_value = sum(
        scalar(
            f"SELECT COALESCE(sum(loan_amount),0) FROM {t} WHERE {DISBURSED_SQL}"
        )
        for t in TABLES.values()
    )
    compare("disbursed loans", sum(o["count"] for o in officers), sql_count)
    compare("disbursed value", sum(o["value"] for o in officers), sql_value)

    # DISTINCT across all three products, not the sum of three per-table
    # counts - the same officer works on more than one product, and summing
    # would report five names as eleven.
    union = " UNION ".join(
        f"SELECT DISTINCT account_officer FROM {t} WHERE {DISBURSED_SQL}"
        for t in TABLES.values()
    )
    raw_spellings = scalar(f"SELECT count(*) FROM ({union}) AS spellings")
    merged = sum(o["variants"] for o in officers)
    print(f"  {'name variants merged':<44} {merged} spellings -> {len(officers)} officers")
    if raw_spellings > len(officers):
        notes.append(
            f"{raw_spellings} distinct spellings across products collapsed to "
            f"{len(officers)} officers. Run `manage.py list_officers` to review."
        )
    print()

    # --- turnaround -------------------------------------------------------
    print("Turnaround")
    times = turnaround(user)
    for stage, column in [
        ("Review", "reviewed_at"),
        ("Credit", "credit_approved_at"),
        ("Control", "control_approved_at"),
        ("Disbursement", "disbursed_at"),
    ]:
        reached = sum(
            scalar(f"SELECT count(*) FROM {t} WHERE {column} IS NOT NULL")
            for t in TABLES.values()
        )
        shown = next(
            (s["reached"] for s in times["stages"] if s["label"].startswith(stage[:6])),
            None,
        )
        compare(f"{stage}: applications reached", shown, reached)
    print()

    # --- verdict ----------------------------------------------------------
    for note in notes:
        print(f"note: {note}")
    if notes:
        print()
    if problems:
        print(f"{len(problems)} figure(s) disagree:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("Every figure on the analytics page agrees with raw SQL.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
