"""Check the presentation's figures against raw SQL.

The presentation reaches its numbers through loans/journey.py: Python over
records loaded via the ORM, with local-day bucketing and gate inference. Any of
that could be subtly wrong and still draw a believable chart. So this asks the
same questions again in plain SQL on its own cursor, with the models and the
journey module outside the path being checked. Agreement means something.

Runs against whichever database .env points at - sample data or production.

    .venv/bin/python scripts/verify_journey.py [days]

Exit status is non-zero if anything disagrees.
"""

import os
import sys
from datetime import timedelta
from pathlib import Path

import django

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth.models import User  # noqa: E402
from django.db import connections  # noqa: E402
from django.utils import timezone  # noqa: E402

from loans import journey  # noqa: E402
from loans.models import PRODUCTS  # noqa: E402

TZ = "Africa/Lagos"


def sql_scalar(query, params):
    with connections["loans"].cursor() as cursor:
        cursor.execute(query, params)
        return cursor.fetchone()[0]


def sum_tables(template, params):
    return sum(sql_scalar(template.format(table=m._meta.db_table), params) for m in PRODUCTS.values())


def main():
    window = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    end = timezone.localdate()
    start = end - timedelta(days=window - 1)
    user = User.objects.filter(is_active=True).first()
    if user is None:
        print("No active Django user exists; create one with manage.py createsuperuser.")
        return 2

    records = journey.load_records(user)
    group = journey.cohort(records, start, end)
    days = journey.daily_activity(records, start, end)
    funnel = journey.funnel(group)
    failures = 0

    def check(label, ours, theirs):
        nonlocal failures
        ok = ours == theirs
        failures += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {label}: presentation={ours} sql={theirs}")

    in_window = f"(({{col}} AT TIME ZONE '{TZ}')::date BETWEEN %s AND %s)"
    print(f"Window {start} to {end} ({window} days)\n")

    print("Daily activity totals")
    for event, column, flag in [
        ("submitted", "created_at", None), ("reviewed", "reviewed_at", "reviewed"),
        ("credit_approved", "credit_approved_at", "credit_approved"),
        ("control_approved", "control_approved_at", "control_approved"),
        ("disbursed", "disbursed_at", "disbursed"), ("rejected", "rejected_at", "rejected"),
        ("correction", "correction_requested_at", None),
    ]:
        cond = in_window.format(col=column) + (f" AND {flag}" if flag else "")
        theirs = sum_tables(f"SELECT COUNT(*) FROM {{table}} WHERE {cond}", [start, end])
        check(event, sum(d[event] for d in days), theirs)

    print("\nFunnel (applications submitted in the window)")
    cohort_cond = in_window.format(col="created_at")
    later = {
        "SUBMITTED": "TRUE",
        "REVIEWED": "(reviewed OR credit_approved OR control_approved OR disbursed)",
        "CREDIT_APPROVED": "(credit_approved OR control_approved OR disbursed)",
        "CONTROL_APPROVED": "(control_approved OR disbursed)",
        "DISBURSED": "disbursed",
    }
    for stage in funnel:
        theirs = sum_tables(
            f"SELECT COUNT(*) FROM {{table}} WHERE {cohort_cond} AND {later[stage['key']]}", [start, end]
        )
        check(stage["label"], stage["count"], theirs)

    print("\nOutcome")
    outcome = journey.outcome_summary(group)
    check("rejected", outcome["rejected"],
          sum_tables(f"SELECT COUNT(*) FROM {{table}} WHERE {cohort_cond} AND rejected", [start, end]))
    check("disbursed (not rejected)", outcome["disbursed"],
          sum_tables(f"SELECT COUNT(*) FROM {{table}} WHERE {cohort_cond} AND disbursed AND NOT rejected", [start, end]))
    check("open corrections now", sum(1 for r in records if r.correction_open),
          sum_tables("SELECT COUNT(*) FROM {table} WHERE correction_requested AND NOT rejected AND NOT disbursed", []))

    print()
    if failures:
        print(f"{failures} figure(s) disagree with raw SQL.")
        return 1
    print("Every checked figure agrees with raw SQL.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
