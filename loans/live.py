"""The live feed: what is happening on each loan, newest first.

The daily report's left-hand side asks for this every few seconds, so unlike
loans/journey.py - which loads every application and counts in Python - this
goes to the database for just the rows it needs: three small queries, one per
product, each returning only applications something has happened to.

An "event" is a step on the approval spine being reached: a row's own
timestamp for it (`reviewed_at`, `disbursed_at`, ...), the same timestamps
every other figure in the dashboard is counted from. So a line here and the
count beside it in the day-by-day table can never disagree about when
something happened.

Like the journey endpoints, this returns name, reference, product, amount and
the officer who acted - never BVN, NIN, date of birth, phone, email, address
or account numbers.
"""

from django.db.models import F, Q
from django.db.models.functions import Greatest
from django.utils import timezone

from .models import PRODUCTS
from .status import status_label

# (event, label, timestamp column, flag that must be set, who did it, what was said)
#
# A milestone counts only when its flag is set, as in journey.Record: a
# timestamp left behind on a step that was later undone is not an event. A
# correction has no flag of its own that survives being resolved - it stays in
# the feed for as long as its timestamp does.
SOURCES = [
    ("submitted", "Submitted", "created_at", None, None, None),
    ("reviewed", "Reviewed", "reviewed_at", "reviewed", "reviewed_by", None),
    ("credit_approved", "Credit approved", "credit_approved_at", "credit_approved", "credit_approved_by", None),
    ("control_approved", "Control approved", "control_approved_at", "control_approved", "control_approved_by", None),
    ("disbursed", "Disbursed", "disbursed_at", "disbursed", "disbursed_by", None),
    ("rejected", "Rejected", "rejected_at", "rejected", "rejected_by", "rejection_reason"),
    ("correction", "Correction requested", "correction_requested_at", None, "correction_requested_by", "correction_message"),
]
STAMPS = [column for _event, _label, column, *_rest in SOURCES]

DEFAULT_LIMIT = 40
MAX_LIMIT = 100
DETAIL_CHARS = 160


def _columns(model):
    wanted = ["id", "reference_no", "first_name", "loan_amount", "status", "_last_name"]
    for _event, _label, column, flag, actor, detail in SOURCES:
        wanted.append(column)
        if flag:
            wanted.append(flag)
        if actor:
            wanted.append(f"{actor}__full_name")
        if detail:
            wanted.append(detail)
    return wanted


def events(user, product=None, since=None, limit=DEFAULT_LIMIT):
    """The newest `limit` events, newest first; only those after `since` if given.

    Applications are ranked by their most recent timestamp and the newest
    `limit` of each product fetched. That is enough to hold the newest `limit`
    events overall: every one of those belongs to an application whose latest
    timestamp is at least as recent as it is.
    """
    limit = max(1, min(int(limit), MAX_LIMIT))
    found = []
    for code, model in PRODUCTS.items():
        if product and product != code:
            continue
        rows = model.objects.visible_to(user).annotate(
            _latest=Greatest(*STAMPS), _last_name=F(model.LAST_NAME_FIELD)
        )
        if since is not None:
            touched = Q()
            for column in STAMPS:
                touched |= Q(**{f"{column}__gt": since})
            rows = rows.filter(touched)
        for row in rows.order_by("-_latest").values(*_columns(model))[:limit]:
            name = " ".join(part for part in (row["first_name"], row["_last_name"]) if part)
            for event, label, column, flag, actor, detail in SOURCES:
                at = row[column]
                if at is None or (flag and not row[flag]):
                    continue
                if since is not None and at <= since:
                    continue
                said = (row[detail] or "").strip() if detail else ""
                found.append({
                    # Enough to tell one event from every other, so a page that
                    # asks again with an overlapping `since` can drop repeats.
                    "key": f"{code}:{row['id']}:{event}:{at.isoformat()}",
                    "event": event,
                    "label": label,
                    "at": timezone.localtime(at).isoformat(),
                    "product": code,
                    "product_label": model.PRODUCT_LABEL,
                    "id": row["id"],
                    "reference": row["reference_no"],
                    "name": name,
                    "amount": float(row["loan_amount"] or 0),
                    "by": row[f"{actor}__full_name"] if actor else None,
                    "detail": said[:DETAIL_CHARS] + ("…" if len(said) > DETAIL_CHARS else ""),
                    "status": row["status"],
                    "status_label": status_label(row["status"]),
                    "_at": at,
                })
    found.sort(key=lambda e: e["_at"], reverse=True)
    for event in found:
        del event["_at"]
    return found[:limit]
