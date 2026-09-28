from collections import defaultdict
from datetime import timedelta
from statistics import median
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from .models import PRODUCTS
from .officers import group_by_officer
from .status import TERMINAL_STATUSES

AMOUNT_BANDS = [
    ("Under 250k", 250_000),
    ("250k - 500k", 500_000),
    ("500k - 1m", 1_000_000),
    ("1m - 5m", 5_000_000),
    ("5m - 20m", 20_000_000),
    ("20m - 50m", 50_000_000),
    ("Over 50m", None),
]

STAGES = [
    ("Review", "reviewed_at"),
    ("Credit", "credit_approved_at"),
    ("Control", "control_approved_at"),
    ("Disbursed", "disbursed_at"),
]

def _querysets(user):
    for code, model in PRODUCTS.items():
        yield code, model, model.objects.visible_to(user)

def volume_by_month(user, months=12):
    since = (timezone.now() - timedelta(days=31*months)).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )

    buckets = defaultdict(lambda: defaultdict(lambda: {"count": 0, "value": Decimal(0)}))
    for code, model, queryset in _querysets(user):
        rows = (
            queryset.filter(created_at__gte=since)
            .annotate(month=TruncMonth("created_at"))
            .values("month")
            .annotate(count=Count("id"), value=Sum("loan_amount"))
            .order_by("month")
        )
        for row in rows:
            if row["month"] is None:
                continue
            bucket = buckets[row["month"].date().replace(day=1)][code]
            bucket["count"] += row["count"]
            bucket["value"] += row["value"] or Decimal(0)


    labels = sorted(buckets)
    return [
        {
            "month": month,
            "label": month.strftime("%b %y"),
            "total": sum(p["count"] for p in buckets[month].values()),
            "value": sum(p["value"] for p in buckets[month].values()),
            "by_product": {code: buckets[month][code]["count"] for code in PRODUCTS},
        }
        for month in labels
    ]

def product_volume(user, months=12):
    """How the three products compare over the period.

    Counts, value and disbursed count per product, largest first. Uses the same
    window as volume_by_month so the two panels agree.
    """
    since = (timezone.now() - timedelta(days=31 * months)).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )

    rows = []
    for _code, model, queryset in _querysets(user):
        summary = queryset.filter(created_at__gte=since).aggregate(
            count=Count("id"),
            value=Sum("loan_amount"),
            disbursed=Count("id", filter=Q(status="DISBURSED")),
        )
        rows.append(
            {
                "label": model.PRODUCT_LABEL,
                "code": model.PRODUCT_CODE,
                "count": summary["count"] or 0,
                "value": summary["value"] or Decimal(0),
                "disbursed": summary["disbursed"] or 0,
            }
        )
    return sorted(rows, key=lambda row: -row["count"])


def amount_bands(user):
    counts = {label: 0 for label, _ceiling in AMOUNT_BANDS}
    labels = {f"band_{index}": label for index, (label, _c) in enumerate(AMOUNT_BANDS)}

    for _code, _model, queryset in _querysets(user):
        filters = {}
        floor = 0
        for index, (_label, ceiling) in enumerate(AMOUNT_BANDS):
            condition = Q(loan_amount__gte=floor)
            if ceiling is not None:
                condition &= Q(loan_amount__lt=ceiling)
                floor = ceiling
            filters[f"band_{index}"] = Count("id", filter=condition)
        for alias, value in queryset.aggregate(**filters).items():
            counts[labels[alias]] += value or 0

    return [{"label": label, "count": counts[label]} for label, _c in AMOUNT_BANDS]

def top_states(user, limit=10):
    totals = defaultdict(int)
    for _code, _model, queryset in _querysets(user):
        for row in queryset.values("state").annotate(count=Count("id")):
            name = (row["state"] or "").strip() or "(not recorded)"
            totals[name.title()] += row["count"]
    ranked = sorted(totals.items(), key=lambda kv: -kv[1])[:limit]
    return [{"label": name, "count": count} for name, count in ranked]

def officer_performance(user, limit=15):
    rows = []
    for _code, _model, queryset in _querysets(user):
        rows.extend(
            queryset.filter(status="DISBURSED")
            .values("account_officer")
            .annotate(count=Count("id"), value=Sum("loan_amount"))
        )
    grouped = group_by_officer(rows)
    ranked = sorted(grouped.items(), key=lambda kv: -(kv[1]["value"] or 0))[:limit]
    return [
        {
            "label": name,
            "count": data["count"],
            "value": data["value"] or Decimal(0),
            "variants": len(data["variants"]),
        }
        for name, data in ranked
    ]

def turnaround(user):
    reached = {label: [] for label, _column in STAGES}
    open_ages = []
    now = timezone.now()

    columns = ["created_at"] + [column for _label, column in STAGES]
    for _code, _model, queryset in _querysets(user):
        for row in queryset.values("status", *columns):
            created = row["created_at"]
            if not created:
                continue
            for label, column in STAGES:
                stamp = row[column]
                if stamp:
                    reached[label].append((stamp - created).total_seconds() / 86400)
            if row["status"] not in TERMINAL_STATUSES:
                open_ages.append((now - created).total_seconds() / 86400)


    stages = [
        {
            "label": label,
            "median_days": round(median(values), 1) if values else None,
            "reached": len(values),
        }
        for label, _column in STAGES
        for values in [reached[label]]
    ] 
    return {
        "stages": stages,
        "open_count": len(open_ages),
        "open_median_age": round(median(open_ages), 1) if open_ages else None,
        "open_oldest": round(max(open_ages), 1) if open_ages else None,
    }