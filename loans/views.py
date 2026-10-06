"""The presentation: one page, and the JSON it is drawn from.

`presentation` renders the shell - tabs, filters, empty chart containers.
Everything on it is filled by the `api_*` endpoints below, which the page
calls with the current date range and product. Each endpoint loads the
visible applications once and hands them to loans/journey.py, where all the
counting happens.

Everything here reads. The router in loans/routers.py raises on any write
attempt, and the `loan_reader` database role holds GRANT SELECT only. Row
visibility still has exactly one definition - `LoanRequestQuerySet.visible_to`
- because `journey.load_records` goes through it.
"""

import logging
from datetime import date, timedelta
from functools import wraps

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import connections
from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.db.utils import Error as DatabaseError
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.utils import timezone

from . import journey
from .models import PRODUCTS, REQUEST_TYPE_BY_MODEL, ApprovalAuditLog, CashForCarRequest, LoanComment
from .status import status_label

audit = logging.getLogger("loans.audit")

MAX_RANGE_DAYS = 731
DEFAULT_RANGE_DAYS = 30
SEARCH_LIMIT = 25


# --- plumbing -----------------------------------------------------------------

def api_view(view):
    """JSON endpoint: 401 rather than a login redirect, 503 when the DB is down.

    A redirect to the sign-in page would come back to fetch() as a 200 HTML
    page and fail somewhere confusing; a 401 lets the page send the user to
    sign in again cleanly.
    """

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Your session has ended. Sign in again."}, status=401)
        try:
            return view(request, *args, **kwargs)
        except DatabaseError:
            return JsonResponse(
                {"error": "The loan database is not reachable right now."}, status=503
            )
        except ValueError as exc:
            return JsonResponse({"error": str(exc)}, status=400)

    return wrapper


def _parse_date(value, default):
    if not value:
        return default
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"Not a date: {value!r}. Use YYYY-MM-DD.")


def _window(request, prefix=""):
    """The [start, end] date range a request asks for, both inclusive."""
    today = timezone.localdate()
    end = _parse_date(request.GET.get(f"{prefix}end"), today)
    start = _parse_date(request.GET.get(f"{prefix}start"), end - timedelta(days=DEFAULT_RANGE_DAYS - 1))
    if start > end:
        start, end = end, start
    if (end - start).days >= MAX_RANGE_DAYS:
        start = end - timedelta(days=MAX_RANGE_DAYS - 1)
    return start, end


def _product(request):
    code = (request.GET.get("product") or "").strip()
    if code in ("", "all"):
        return None
    if code not in PRODUCTS:
        raise ValueError(f"No such loan product: {code}")
    return code


def _records(request):
    return journey.load_records(request.user, _product(request))


# --- the page -----------------------------------------------------------------

@login_required
def presentation(request):
    try:
        with connections["loans"].cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError as exc:
        return render(
            request,
            "loans/db_error.html",
            {"error": exc, "host": settings.DATABASES["loans"]["HOST"], "sample": settings.SAMPLE_DB},
            status=503,
        )
    config = {
        "products": [{"code": code, "label": model.PRODUCT_LABEL} for code, model in PRODUCTS.items()],
        "today": timezone.localdate().isoformat(),
        "defaultDays": DEFAULT_RANGE_DAYS,
        "maxDays": MAX_RANGE_DAYS,
        "stalledDays": journey.STALLED_DAYS,
        "events": [{"key": k, "label": l} for k, l in journey.EVENTS],
    }
    return render(
        request,
        "loans/presentation.html",
        {"config": config, "sample": settings.SAMPLE_DB},
    )


# --- data ---------------------------------------------------------------------

PAGE_TABS = {"overview", "monthly", "daily", "flow", "journey", "compare"}
# Insights written for analysis pages that were folded away point at the page
# that now carries that information.
INSIGHT_TAB = {"funnel": "overview", "rejections": "overview"}


def _commission(user, start, end):
    """Cash for Car's fee split between Dash and Floauto, on disbursed loans.

    Counted on disbursed loans only - a pending or rejected request has not
    paid anyone anything yet. Also: on how many loans did each party take
    the bigger share? A missing share counts as 0. The total can be swung by
    a few large loans; the per-loan count shows who usually comes out ahead.
    """
    qs = CashForCarRequest.objects.visible_to(user).filter(
        status="DISBURSED", created_at__date__range=(start, end)
    )
    zero = Value(0, output_field=DecimalField())
    totals = qs.aggregate(dash=Sum("fee_dash_share"), floauto=Sum("fee_floauto_share"))
    per_loan = qs.annotate(
        _dash=Coalesce("fee_dash_share", zero), _floauto=Coalesce("fee_floauto_share", zero)
    ).aggregate(
        dash_higher=Count("pk", filter=Q(_dash__gt=F("_floauto"))),
        floauto_higher=Count("pk", filter=Q(_floauto__gt=F("_dash"))),
        equal=Count("pk", filter=Q(_floauto=F("_dash"))),
    )
    return {
        "loans": qs.count(),
        "dash": float(totals["dash"] or 0),
        "floauto": float(totals["floauto"] or 0),
        "dash_higher": per_loan["dash_higher"],
        "floauto_higher": per_loan["floauto_higher"],
        "equal": per_loan["equal"],
    }


@api_view
def api_dashboard(request):
    """Everything every page of the deck shows, in one round trip.

    `period=all` (the default view) covers every application in the database;
    otherwise `start`/`end` set the window. Each page's figures count the
    applications submitted in the window; the daily pages count what happened
    on each day.
    """
    records = journey.load_records(request.user)
    span = journey.data_span(records)
    today = timezone.localdate()
    if request.GET.get("period", "all") == "all":
        start = date.fromisoformat(span["first"]) if span["first"] else today
        end = max(today, date.fromisoformat(span["last"])) if span["last"] else today
    else:
        start, end = _window(request)
    now = timezone.now()
    group = journey.cohort(records, start, end)

    scopes = {"all": journey.dashboard_block(group, start, end)}
    daily = {"all": journey.flow(records, start, end)}
    for code in PRODUCTS:
        scopes[code] = journey.dashboard_block([r for r in group if r.product == code], start, end)
        daily[code] = journey.flow([r for r in records if r.product == code], start, end)

    insight_list = []
    for item in journey.insights(records, start, end, now):
        tab = INSIGHT_TAB.get(item["tab"], item["tab"])
        item["tab"] = tab if tab in PAGE_TABS else None
        insight_list.append(item)

    return JsonResponse({
        "window": {"start": start.isoformat(), "end": end.isoformat(),
                   "all": request.GET.get("period", "all") == "all"},
        "data": span,
        "database": {"name": settings.DATABASES["loans"]["NAME"], "sample": settings.SAMPLE_DB},
        "products": journey.product_split(group),
        "scopes": scopes,
        "daily": daily,
        "commission": _commission(request.user, start, end),
        "insights": insight_list,
    })


@api_view
def api_day(request):
    """What happened on one day - the daily bar chart's drill-down."""
    day = _parse_date(request.GET.get("date"), timezone.localdate())
    return JsonResponse(journey.day_detail(_records(request), day))


@api_view
def api_drill(request):
    """The applications behind any clicked chart element."""
    if request.GET.get("period") == "all":
        start, end = date(1900, 1, 1), timezone.localdate() + timedelta(days=1)
    else:
        start, end = _window(request)
    kind = request.GET.get("kind", "")
    key = request.GET.get("key", "")
    title, rows = journey.drill(_records(request), kind, key, start, end)
    if not title:
        raise ValueError(f"Unknown drill-down: {kind}")
    return JsonResponse({"title": title, "count": len(rows), "rows": rows[:500],
                         "truncated": len(rows) > 500})


@api_view
def api_compare(request):
    a = _window(request, "a_")
    b = _window(request, "b_")
    return JsonResponse(journey.compare(_records(request), a, b))


# --- the journey of one application -------------------------------------------

@api_view
def api_search(request):
    """Find applications by reference number or applicant name."""
    q = (request.GET.get("q") or "").strip()
    product = _product(request)
    recent = len(q) < 2   # nothing typed yet: show the latest applications
    rows = []
    for code, model in PRODUCTS.items():
        if product and code != product:
            continue
        terms = q.split()
        if recent:
            condition = Q()
        else:
            condition = Q(reference_no__icontains=q) | Q(first_name__icontains=q) | Q(
                **{f"{model.LAST_NAME_FIELD}__icontains": q}
            )
        if not recent and len(terms) > 1:
            condition |= Q(first_name__icontains=terms[0]) & Q(
                **{f"{model.LAST_NAME_FIELD}__icontains": terms[-1]}
            )
        found = (
            model.objects.visible_to(request.user)
            .filter(condition)
            .annotate(last_value=F(model.LAST_NAME_FIELD))
            .values("id", "reference_no", "first_name", "last_value", "loan_amount", "status", "created_at")
            .order_by("-created_at")[:SEARCH_LIMIT]
        )
        for row in found:
            rows.append({
                "product": code,
                "product_label": model.PRODUCT_LABEL,
                "id": row["id"],
                "reference": row["reference_no"],
                "name": " ".join(p for p in (row["first_name"], row["last_value"]) if p),
                "amount": float(row["loan_amount"] or 0),
                "status": row["status"],
                "status_label": status_label(row["status"]),
                "submitted": timezone.localtime(row["created_at"]).isoformat() if row["created_at"] else None,
            })
    rows.sort(key=lambda r: r["submitted"] or "", reverse=True)
    return JsonResponse({"rows": rows[:SEARCH_LIMIT]})


def _actor(admin):
    return admin.full_name if admin else None


@api_view
def api_journey(request, product, pk):
    """One application's path through the pipeline, step by step."""
    if product not in PRODUCTS:
        raise Http404
    model = PRODUCTS[product]
    loan = (
        model.objects.visible_to(request.user)
        .select_related("reviewed_by", "credit_approved_by", "control_approved_by",
                        "disbursed_by", "rejected_by", "correction_requested_by")
        .filter(pk=pk)
        .first()
    )
    if loan is None:
        return JsonResponse({"error": "No such application."}, status=404)

    audit.info(
        "JOURNEY user=%s ip=%s product=%s loan=%s ref=%s",
        request.user.get_username(), request.META.get("REMOTE_ADDR", "?"),
        product, loan.pk, loan.reference_no,
    )

    now = timezone.now()
    steps = [{
        "key": "SUBMITTED", "label": "Submitted", "state": "done",
        "at": journey._iso(loan.created_at), "by": None, "note": None,
    }]
    previous_at = loan.created_at
    blocked = False
    for key, label, flag, column in journey.MILESTONES[1:]:
        actor_field = {"reviewed": "reviewed_by", "credit_approved": "credit_approved_by",
                       "control_approved": "control_approved_by", "disbursed": "disbursed_by"}[flag]
        note_field = {"reviewed": "review_note", "credit_approved": "credit_note",
                      "control_approved": "control_note", "disbursed": "disbursement_note"}[flag]
        at = getattr(loan, column)
        if getattr(loan, flag):
            took = journey._days(at - previous_at) if at and previous_at else None
            steps.append({"key": key, "label": label, "state": "done", "at": journey._iso(at),
                          "by": _actor(getattr(loan, actor_field)), "note": getattr(loan, note_field),
                          "took_days": round(took, 1) if took is not None else None})
            previous_at = at or previous_at
        else:
            if blocked:
                state = "future"
            elif loan.rejected:
                state = "blocked"
            elif loan.correction_requested:
                state = "correction"
            else:
                state = "current"
            waiting = journey._days(now - previous_at) if (state == "current" and previous_at) else None
            steps.append({"key": key, "label": label, "state": state, "at": None, "by": None,
                          "note": None, "waiting_days": round(waiting, 1) if waiting is not None else None})
            blocked = True

    rejected_gate = None
    if loan.rejected:
        furthest = max([0] + [i for i, (_k, _l, flag, _c) in enumerate(journey.MILESTONES[1:], 1)
                              if getattr(loan, flag)])
        rejected_gate = journey.GATES[min(furthest, len(journey.GATES) - 1)]

    request_type = REQUEST_TYPE_BY_MODEL[model]
    history = [
        {"action": h.action, "from": h.from_status, "to": h.to_status, "note": h.note,
         "by": _actor(h.actor), "at": journey._iso(h.created_at)}
        for h in ApprovalAuditLog.objects.filter(request_type=request_type, request_id=loan.pk)
        .select_related("actor").order_by("created_at")
    ]
    comments = [
        {"body": c.body, "by": _actor(c.author), "at": journey._iso(c.created_at),
         "recommendation": c.is_recommendation}
        for c in LoanComment.objects.filter(request_type=request_type, request_id=loan.pk)
        .select_related("author").order_by("created_at")
    ]
    end_at = loan.disbursed_at if loan.disbursed else (loan.rejected_at if loan.rejected else now)
    total_days = journey._days(end_at - loan.created_at) if end_at and loan.created_at else None

    return JsonResponse({
        "product": product,
        "product_label": model.PRODUCT_LABEL,
        "id": loan.pk,
        "reference": loan.reference_no,
        "name": loan.full_name,
        "amount": float(loan.loan_amount or 0),
        "tenor_months": loan.tenor_months,
        "purpose": loan.loan_purpose,
        "officer": loan.account_officer,
        "state": loan.state,
        "status": loan.status,
        "status_label": status_label(loan.status),
        "total_days": round(total_days, 1) if total_days is not None else None,
        "closed": bool(loan.disbursed or loan.rejected),
        "steps": steps,
        "rejection": {
            "at": journey._iso(loan.rejected_at), "by": _actor(loan.rejected_by),
            "reason": loan.rejection_reason, "stage": loan.rejected_stage,
            "gate": journey.GATE_LABELS.get(rejected_gate),
        } if loan.rejected else None,
        "correction": {
            "open": loan.correction_requested, "count": loan.correction_count,
            "at": journey._iso(loan.correction_requested_at), "by": _actor(loan.correction_requested_by),
            "message": loan.correction_message, "stage": loan.correction_stage,
        } if (loan.correction_requested or loan.correction_count) else None,
        "history": history,
        "comments": comments,
    })


# --- health -------------------------------------------------------------------

def healthz(request):
    """Liveness and database reachability, for the service manager and monitoring.

    Deliberately unauthenticated so a monitor can reach it, and deliberately
    uninformative: it reports whether the loan database answers, never what is
    in it and never the connection error itself.
    """
    payload = {"app": "ok", "database": "ok"}
    status = 200
    try:
        with connections["loans"].cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError:
        payload["database"] = "unreachable"
        status = 503
    return JsonResponse(payload, status=status)
