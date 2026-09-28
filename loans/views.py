"""Views for browsing, exporting and inspecting loan applications.

Everything here reads. The router in loans/routers.py raises on any write
attempt, and the `loan_reader` database role holds GRANT SELECT only.

Row visibility has exactly one definition - `LoanRequestQuerySet.visible_to`.
Lists, details, exports and document downloads all go through it, so a future
per-user rule cannot be applied to the screen but forgotten on the download.
"""

import base64
import binascii
import logging
import re
from datetime import datetime
from functools import wraps
from urllib.parse import quote

from django.conf import settings
from django.core.paginator import Paginator
from django.db import connections
from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.utils import Error as DatabaseError
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse 
from django.utils import timezone
from django.views.generic import DetailView
from django_filters.views import FilterView

from .filters import filter_for
from .models import (
    PRODUCTS,
    REQUEST_TYPE_BY_MODEL,
    ApprovalAuditLog,
    CashForCarRequest,
    LoanComment,
    RequestDocument,
)
from .status import (
    PENDING,
    PIPELINE_LENGTH,
    PIPELINE_POSITION,
    STATUS_CHOICES,
    STATUS_GROUP_LABELS,
    STATUS_GROUPS,
    TERMINAL_STATUSES,
    status_label,
)

from .analytics import (
    amount_bands,
    officer_performance,
    product_volume,
    top_states,
    turnaround,
    volume_by_month,
)

from .charts import SERIES_COLOURS, bar_chart, column_chart, money, pie_chart

audit = logging.getLogger("loans.audit")

PAGE_SIZES = [25, 50, 100, 200]
DEFAULT_PAGE_SIZE = 50
NON_FILTER_PARAMS = {"page", "sort", "per_page"}

# The filters people reach for every time stay on screen; the rest sit behind a
# disclosure. Ten controls in one row reads as a form to be filled in rather
# than a set of options, and nobody uses the last six.
PRIMARY_FILTERS = ["q", "status", "created_from", "created_to"]

# Columns shown in the list tables. Shared across products so the unified view
# and the per-product views read the same way.
LIST_COLUMNS = [
    ("created_at", "Created"),
    ("reference_no", "Reference"),
    ("applicant", "Applicant"),
    ("loan_amount", "Amount"),
    ("status", "Status"),
    ("account_officer", "Officer"),
]
SORTABLE = {"created_at", "reference_no", "loan_amount", "status", "account_officer"}


PII_COLUMNS = {
    "bvn",
    "nin",
    "date_of_birth",
    "mothers_maiden_name",
    "email",
    "phone",
    "residential_address",
    "next_of_kin_name",
    "next_of_kin_phone",
    "next_of_kin_relation",
    "account_no",
    "account_number",
    "salary_account_no",
    "ippis_number",
    "employee_no",
}


INLINE_MIME_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/gif",
    "image/webp",
}

DATE_COLUMNS_SUFFIX = ("_at", "_date")
AMOUNT_HINTS = ("amount", "valuation", "income", "balance", "salary", "share", "pay")

COLUMN_WIDTHS = {
    "reference_no": 18, "first_name": 20, "surname": 20, "last_name": 20,
    "middle_name": 20, "email": 30, "phone": 16, "account_officer": 24,
    "residential_address": 40, "loan_purpose": 40, "created_at": 20,
    "updated_at": 20, "status": 22, "loan_amount": 16, "bvn": 14, "nin": 14,
}
DEFAULT_WIDTH = 18
DATE_FORMAT = "yyyy-mm-dd hh:mm:ss"
AMOUNT_FORMAT = "#,##0.00"


# --- error handling -----------------------------------------------------------

def handle_db_errors(view):
    """Render the connectivity page instead of a 500 when the database is down."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except DatabaseError as exc:
            return _db_error(request, exc)

    return wrapper


def _db_error(request, exc):
    return render(
        request,
        "loans/db_error.html",
        {"error": exc, "host": settings.DATABASES["loans"]["HOST"], "sample": settings.SAMPLE_DB},
        status=503,
    )


class DatabaseErrorMixin:
    """Render a readable page when the loan database is unreachable.

    Without this a dropped tunnel produces a raw 500, which reads like an
    application bug rather than a connectivity problem.
    """

    def get(self, request, *args, **kwargs):
        try:
            response = super().get(request, *args, **kwargs)
            if hasattr(response, "render"):
                response.render()
            return response
        except DatabaseError as exc:
            return _db_error(request, exc)


# --- helpers ------------------------------------------------------------------

def compact_number(value):
    """1284 -> 1,284 | 1284000 -> 1.3M. For stat tiles, not table columns."""
    if value is None:
        return "0"
    number = float(value)
    for limit, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(number) >= limit:
            trimmed = f"{number / limit:.1f}".removesuffix(".0")
            return f"{trimmed}{suffix}"
    return f"{number:,.0f}"


def _money_sum(field="loan_amount"):
    """SUM that returns 0 rather than None for an empty set."""
    return Coalesce(Sum(field), Value(0), output_field=DecimalField(max_digits=20, decimal_places=4))


def _summarise(queryset):
    """Counts and value for one product's filtered rows."""
    return queryset.aggregate(
        total=Count("id"),
        value=_money_sum(),
        disbursed=Count("id", filter=Q(status="DISBURSED")),
        rejected=Count("id", filter=Q(status="REJECTED")),
        open=Count("id", filter=~Q(status__in=TERMINAL_STATUSES)),
    )


def _export_columns(model):
    """Every mapped column, in model order, with actor ids resolved to names.

    Derived from the model rather than hand-listed, so a column added upstream
    reaches the export as soon as it reaches the model - the payment requests
    portal had to maintain this list by hand and it drifted.
    """
    columns = []
    for field in model._meta.fields:
        if field.is_relation:
            columns.append(f"{field.name}__full_name")
        else:
            columns.append(field.name)
        # `status` is a query annotation, not a column, so it is not in
        # _meta.fields - but it is the column a status report is actually
        # about. Place it beside the reference rather than leaving it out.
        if field.name == "reference_no":
            columns.append("status")
    return columns


def _header(column):
    return column.replace("__full_name", "").replace("_", " ").capitalize()


def _is_pii(column):
    return column.split("__")[0] in PII_COLUMNS


def _cell(value, column):
    """Render one value for export."""
    if not settings.EXPORT_INCLUDE_PII and _is_pii(column) and value not in (None, ""):
        return "***"
    if value is None:
        return ""
    if column == "status":
        return status_label(value)
    return value


# --- dashboard ----------------------------------------------------------------

@login_required
@handle_db_errors
def dashboard(request):
    """Totals across all three products, and per product."""
    products = []
    totals = {"total": 0, "value": 0, "disbursed": 0, "rejected": 0, "open": 0}
    status_totals = {code: 0 for code, _label in STATUS_CHOICES}

    for code, model in PRODUCTS.items():
        queryset = model.objects.visible_to(request.user)
        summary = _summarise(queryset)
        products.append(
            {
                "code": code,
                "label": model.PRODUCT_LABEL,
                "summary": summary,
                "value_compact": compact_number(summary["value"]),
            }
        )
        for key in totals:
            totals[key] += summary[key] or 0
        for row in queryset.values("status").annotate(n=Count("id")):
            status_totals[row["status"]] = status_totals.get(row["status"], 0) + row["n"]

    status_breakdown = [
        {
            "code": code,
            "label": label,
            "count": status_totals.get(code, 0),
            "pct": round(status_totals.get(code, 0) * 100 / totals["total"]) if totals["total"] else 0,
        }
        for code, label in STATUS_CHOICES
    ]

    return render(
        request,
        "loans/dashboard.html",
        {
            "products": products,
            "totals": totals,
            "total_value_compact": compact_number(totals["value"]),
            "status_breakdown": status_breakdown,
            "sample": settings.SAMPLE_DB,
        },
    )

# --- shared list behaviour ----------------------------------------------------

class ListContextMixin:
    """Sort links, filter chips and page-size links, shared by the list views."""

    def _query(self, **overrides):
        """Current query string with `overrides` applied and blanks dropped."""
        params = self.request.GET.copy()
        for key, value in overrides.items():
            if value is None:
                params.pop(key, None)
            else:
                params[key] = value
        for key in [k for k, v in params.items() if not v]:
            params.pop(key)
        return params.urlencode()

    def _sort_links(self):
        """Header links that toggle asc/desc, keeping filters intact."""
        current = self.request.GET.get("sort", "")
        links = {}
        for name, _label in LIST_COLUMNS:
            if name not in SORTABLE:
                continue
            ascending = current == name
            links[name] = {
                "url": self._query(sort=f"-{name}" if ascending else name, page=None),
                "direction": "asc" if ascending else ("desc" if current == f"-{name}" else ""),
            }
        return links

    def _active_filters(self, form=None):
        """One chip per applied filter, each with a link that removes it."""
        chips = []
        for name, value in self.request.GET.items():
            if name in NON_FILTER_PARAMS or not value:
                continue
            field = form.fields.get(name) if form else None
            label = getattr(field, "label", None) or name.replace("_", " ").capitalize()
            try:
                choices = {str(k): v for k, v in getattr(field, "choices", ())}
            except (TypeError, ValueError):
                choices = {}
            chips.append(
                {
                    "label": label,
                    "value": choices.get(value, value),
                    "remove_url": self._query(**{name: None, "page": None}),
                }
            )
        return chips

    def _page_size(self):
        try:
            size = int(self.request.GET.get("per_page", DEFAULT_PAGE_SIZE))
        except (TypeError, ValueError):
            return DEFAULT_PAGE_SIZE
        return size if size in PAGE_SIZES else DEFAULT_PAGE_SIZE

    def _shared_context(self, context, form=None):
        context["list_columns"] = LIST_COLUMNS
        context["sortable"] = SORTABLE
        context["sort_links"] = self._sort_links()
        context["active_filters"] = self._active_filters(form)
        context["applied_filter_count"] = len(context["active_filters"])
        context["page_sizes"] = PAGE_SIZES
        context["per_page"] = self._page_size()
        context["page_size_links"] = {
            size: self._query(per_page=size, page=None) for size in PAGE_SIZES
        }
        params = self.request.GET.copy()
        params.pop("page", None)
        context["filter_query"] = params.urlencode()
        context["filter_form"] = form
        if form is not None:
            fields = {f.name: f for f in form}
            context["search_field"] = fields.get("q")
            context["primary_filters"] = [
                fields[name] for name in PRIMARY_FILTERS
                if name in fields and name != "q"
            ]
            context["more_filters"] = [
                f for name, f in fields.items()
                if name not in PRIMARY_FILTERS and name != "sort"
            ]
        context["sample"] = settings.SAMPLE_DB
        return context


class ProductListView(LoginRequiredMixin, DatabaseErrorMixin, ListContextMixin, FilterView):
    """One loan product, with its full filter set and export."""

    template_name = "loans/list.html"
    context_object_name = "requests"

    @property
    def model(self):
        code = self.kwargs["product"]
        if code not in PRODUCTS:
            raise Http404(f"No such loan product: {code}")
        return PRODUCTS[code]

    def get_filterset_class(self):
        return filter_for(self.model)

    def get_queryset(self):
        return self.model.objects.visible_to(self.request.user)

    def get_paginate_by(self, queryset):
        return self._page_size()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        self._shared_context(context, self.filterset.form)
        model = self.model
        context["product"] = model.PRODUCT_CODE
        context["product_label"] = model.PRODUCT_LABEL
        context["unified"] = False

        summary = _summarise(self.filterset.qs)
        context["summary"] = summary
        context["value_compact"] = compact_number(summary["value"])

        page = context.get("page_obj")
        if page and summary["total"]:
            context["range_start"] = page.start_index()
            context["range_end"] = page.end_index()
        return context


@login_required
@handle_db_errors
def all_requests(request):
    """Every product in one list, on the fields they share.

    The three tables cannot be UNIONed in the database - their columns differ -
    so each is filtered separately and the results are merged here. That is
    only reasonable because the whole dataset is around 700 rows; if these
    tables grow into the hundreds of thousands, this becomes a database view
    with a UNION ALL behind it rather than a Python merge.
    """
    rows = []
    summary = {"total": 0, "value": 0, "disbursed": 0, "rejected": 0, "open": 0}
    form = None

    for code, model in PRODUCTS.items():
        filterset = filter_for(model)(
            request.GET, queryset=model.objects.visible_to(request.user)
        )
        form = form or filterset.form
        queryset = filterset.qs.annotate(applicant_last_name=F(model.LAST_NAME_FIELD))
        product_summary = _summarise(queryset)
        for key in summary:
            summary[key] += product_summary[key] or 0

        for row in queryset.values(
            "id", "reference_no", "first_name", "applicant_last_name",
            "loan_amount", "status", "created_at", "account_officer", "state",
        ):
            # Keys deliberately match the model's attribute names, so one
            # template renders both these dicts and real model instances -
            # Django resolves `row.full_name` as a dict key or an attribute.
            row["product_code"] = code
            row["product_label"] = model.PRODUCT_LABEL
            row["status_display"] = status_label(row["status"])
            row["full_name"] = " ".join(
                p for p in (row["first_name"], row["applicant_last_name"]) if p
            )
            rows.append(row)

    sort = request.GET.get("sort") or "-created_at"
    reverse = sort.startswith("-")
    key = sort.lstrip("-")
    if key not in SORTABLE:
        key, reverse = "created_at", True
    rows.sort(key=lambda r: (r.get(key) is None, r.get(key)), reverse=reverse)

    helper = ListContextMixin()
    helper.request = request
    paginator = Paginator(rows, helper._page_size())
    page = paginator.get_page(request.GET.get("page"))

    context = helper._shared_context(
        {
            "requests": page.object_list,
            "page_obj": page,
            "paginator": paginator,
            "is_paginated": page.has_other_pages(),
            "product": "all",
            "product_label": "All products",
            "unified": True,
            "summary": summary,
            "value_compact": compact_number(summary["value"]),
            "range_start": page.start_index() if summary["total"] else None,
            "range_end": page.end_index() if summary["total"] else None,
        },
        form,
    )
    return render(request, "loans/list.html", context)


# --- detail -------------------------------------------------------------------

class ProductDetailView(LoginRequiredMixin, DatabaseErrorMixin, DetailView):
    template_name = "loans/detail.html"
    context_object_name = "loan"

    @property
    def model(self):
        code = self.kwargs["product"]
        if code not in PRODUCTS:
            raise Http404(f"No such loan product: {code}")
        return PRODUCTS[code]

    def get_queryset(self):
        return self.model.objects.visible_to(self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        loan = self.object
        model = self.model
        request_type = REQUEST_TYPE_BY_MODEL[model]

        skip = {"id"}
        groups, seen = [], set()
        spine = {
            "reviewed", "reviewed_by", "reviewed_at", "review_note",
            "credit_approved", "credit_approved_by", "credit_approved_at", "credit_note",
            "control_approved", "control_approved_by", "control_approved_at", "control_note",
            "disbursed", "disbursed_by", "disbursed_at", "disbursement_note",
            "rejected", "rejected_by", "rejected_at", "rejection_reason", "rejected_stage",
            "correction_requested", "correction_message", "correction_requested_by",
            "correction_requested_at", "correction_count", "correction_stage",
        }
        applicant = {
            "reference_no", "title", "first_name", "middle_name", "surname", "last_name",
            "gender", "sex", "date_of_birth", "marital_status", "bvn", "nin",
            "mothers_maiden_name", "nationality", "email", "phone",
            "residential_address", "state", "lga",
        }
        loan_terms = {
            "loan_amount", "tenor_months", "loan_purpose", "account_officer",
            "bank_name", "account_no", "account_number", "salary_bank_name",
            "salary_account_no", "repayment_source", "security_details",
        }
        kin = {"next_of_kin_name", "next_of_kin_phone", "next_of_kin_relation"}

        def rows_for(names):
            rows = []
            for field in model._meta.fields:
                if field.name in skip or field.name in seen or field.name not in names:
                    continue
                seen.add(field.name)
                value = getattr(loan, field.name)
                masked = not settings.EXPORT_INCLUDE_PII and _is_pii(field.name) and value
                rows.append(
                    {
                        "label": field.verbose_name.capitalize(),
                        "value": "***" if masked else value,
                        "name": field.name,
                        "masked": bool(masked),
                    }
                )
            return rows

        for title, names in (
            ("Applicant", applicant),
            ("Loan", loan_terms),
            ("Next of kin", kin),
            ("Approval", spine),
        ):
            group = rows_for(names)
            if group:
                groups.append((title, group))

        remaining = rows_for({f.name for f in model._meta.fields} - skip)
        if remaining:
            groups.append((f"{model.PRODUCT_LABEL} detail", remaining))

        context["groups"] = groups
        context["product"] = model.PRODUCT_CODE
        context["product_label"] = model.PRODUCT_LABEL
        context["status_code"] = getattr(loan, "status", loan.derived_status)
        context["status_display"] = status_label(context["status_code"])
        context["pipeline_position"] = PIPELINE_POSITION.get(context["status_code"])
        context["pipeline_length"] = PIPELINE_LENGTH
        context["documents"] = RequestDocument.objects.filter(
            request_type=request_type, request_id=loan.pk
        )

        assessment_entries = []
        for note_field, actor_field, timestamp_field, label in (
            ("review_note", "reviewed_by", "reviewed_at", "Review"),
            ("credit_note", "credit_approved_by", "credit_approved_at", "Credit"),
            ("control_note", "control_approved_by", "control_approved_at", "Control"),
            ("disbursement_note", "disbursed_by", "disbursed_at", "Disbursement"),
            ("rejection_reason", "rejected_by", "rejected_at", "Rejection"),
        ):
            text = getattr(loan, note_field, None)
            if text:
                assessment_entries.append({
                    "author": getattr(loan, actor_field, None),
                    "created_at": getattr(loan, timestamp_field, None) or loan.created_at,
                    "body": text,
                    "label": label,
                })

        recommendation_comments = list(
            LoanComment.objects.filter(
                request_type=request_type,
                request_id=loan.pk,
                is_recommendation=True,
            ).select_related("author")
        )
        for comment in recommendation_comments:
            assessment_entries.append({
                "author": comment.author,
                "created_at": comment.created_at,
                "body": comment.body,
                "label": "Assessment",
            })

        assessment_entries.sort(key=lambda item: item["created_at"] or loan.created_at, reverse=True)
        context["officer_assessment"] = assessment_entries
        context["comments"] = LoanComment.objects.filter(
            request_type=request_type, request_id=loan.pk
        ).select_related("author")
        context["history"] = ApprovalAuditLog.objects.filter(
            request_type=request_type, request_id=loan.pk
        ).select_related("actor")
        context["sample"] = settings.SAMPLE_DB
        return context


# --- documents ----------------------------------------------------------------

@login_required
@handle_db_errors
def document(request, product, pk, doc_id):
    """Serve one uploaded document.

    Three things matter here, and all three are deliberate:

    1. The document is fetched by its parent application, not by id alone. A
       document id on its own says nothing about who may see it; resolving the
       parent through `visible_to` means the row-visibility rule governs
       documents too, automatically, including any rule added later.
    2. `mime_type` is whatever the upload set, so it is attacker-influenced.
       Only a short whitelist is served inline; everything else is forced to
       download as application/octet-stream. Serving an uploaded HTML file
       inline would execute it in the viewer's session - stored XSS with a
       live session cookie next to it.
    3. Every access is logged. These are ID scans and bank statements; who
       opened which one is exactly what an investigation would need.
    """
    if product not in PRODUCTS:
        raise Http404(f"No such loan product: {product}")
    model = PRODUCTS[product]

    loan = get_object_or_404(model.objects.visible_to(request.user), pk=pk)

    document = get_object_or_404(
        RequestDocument.objects.with_content(
            pk=doc_id,
            request_type=REQUEST_TYPE_BY_MODEL[model],
            request_id=loan.pk,
        )
    )

    payload = document.data or ""
    # Stored values are sometimes bare base64 and sometimes a full data: URI.
    if payload.startswith("data:") and "," in payload:
        payload = payload.split(",", 1)[1]
    try:
        content = base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError):
        audit.warning(
            "DOCUMENT_CORRUPT user=%s ip=%s product=%s loan=%s doc=%s",
            request.user.get_username(),
            request.META.get("REMOTE_ADDR", "?"),
            product, loan.pk, document.pk,
        )
        raise Http404("This document could not be decoded.")

    mime = (document.mime_type or "").lower().split(";")[0].strip()
    inline = mime in INLINE_MIME_TYPES
    filename = _safe_filename(
        document.file_name or f"{document.doc_type}-{document.pk}"
    )

    audit.info(
        "DOCUMENT user=%s ip=%s product=%s loan=%s ref=%s doc=%s type=%s mime=%s bytes=%d disposition=%s",
        request.user.get_username(),
        request.META.get("REMOTE_ADDR", "?"),
        product, loan.pk, loan.reference_no, document.pk,
        document.doc_type, mime or "(none)", len(content),
        "inline" if inline else "attachment",
    )

    response = HttpResponse(
        content, content_type=mime if inline else "application/octet-stream"
    )
    disposition = "inline" if inline else "attachment"
    # Both forms: a sanitised ASCII fallback for old clients, and RFC 5987
    # percent-encoding for the real name.
    response["Content-Disposition"] = (
        f'{disposition}; filename="{filename}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )
    # Belt and braces alongside the global nosniff header: never let a browser
    # decide for itself what an uploaded file is.
    response["X-Content-Type-Options"] = "nosniff"
    # The strictest policy the platform offers, on the one response that
    # carries bytes somebody else uploaded. `sandbox` costs nothing for a PDF
    # or an image and removes scripting, plugins and same-origin access
    # entirely if anything else ever slips through the mime whitelist.
    response["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self' data:"
    return response


# --- export -------------------------------------------------------------------

def _filename(product, extension):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"loan-{product}-{stamp}.{extension}"


FILENAME_SAFE = re.compile(r"[^A-Za-z0-9 ._()\[\]-]")


def _safe_filename(name):
    """Make an uploaded filename safe to put in a response header.

    `file_name` is supplied by whoever uploaded the document, so it reaches us
    as attacker-influenced text. A double quote would close the quoted string
    in Content-Disposition early and let the rest be read as further header
    parameters; a newline would attempt header injection outright. Django
    blocks the newline case, but the quote it would happily send.

    Everything outside a conservative whitelist is replaced, path separators
    included, so a name can never traverse or masquerade as a directory.
    """
    name = FILENAME_SAFE.sub("_", (name or "").strip())[:120]
    return name or "document"


def _naive_local(value):
    """Convert an aware datetime to Lagos local time, then drop the tzinfo.

    Two things are going on, and both are necessary:

    * Excel cannot store a timezone-aware datetime at all - openpyxl raises.
    * With USE_TZ on, the database hands back UTC. Writing that to the sheet
      naively would put every timestamp an hour behind what the application
      shows on screen, which nobody would notice until a report disagreed with
      the portal by exactly one hour.
    """
    if hasattr(value, "tzinfo") and value.tzinfo is not None:
        return timezone.localtime(value).replace(tzinfo=None)
    return value


def _column_format(column):
    base = column.split("__")[0]
    if base.endswith("_at") or base in {"date_of_birth", "date_first_appointed"}:
        return "date"
    if any(hint in base for hint in AMOUNT_HINTS):
        return "amount"
    return None


@login_required
@handle_db_errors
def export_xlsx(request, product):
    """Export one product's current filter selection as a formatted workbook."""
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    if product not in PRODUCTS:
        raise Http404(f"No such loan product: {product}")
    model = PRODUCTS[product]

    filterset = filter_for(model)(
        request.GET, queryset=model.objects.visible_to(request.user)
    )
    columns = _export_columns(model)

    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet(model.PRODUCT_LABEL[:31])

    for index, name in enumerate(columns, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = COLUMN_WIDTHS.get(
            name.split("__")[0], DEFAULT_WIDTH
        )
    sheet.freeze_panes = "A2"

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="4F1A60")
    header = []
    for name in columns:
        cell = WriteOnlyCell(sheet, value=_header(name))
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center")
        header.append(cell)
    sheet.append(header)

    rows = filterset.qs.values_list(*columns)
    _audit_export(request, product, "xlsx", rows.count())

    for row in rows.iterator(chunk_size=500):
        cells = []
        for value, name in zip(row, columns):
            value = _cell(value, name)
            if hasattr(value, "quantize"):
                value = float(value)
            value = _naive_local(value)
            fmt = _column_format(name)
            # A masked value is the string "***" and must not be given a date
            # or number format, or Excel shows it as a broken cell.
            if fmt and not isinstance(value, str):
                cell = WriteOnlyCell(sheet, value=value)
                cell.number_format = DATE_FORMAT if fmt == "date" else AMOUNT_FORMAT
                cells.append(cell)
            else:
                cells.append(value)
        sheet.append(cells)

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = f'attachment; filename="{_filename(product, "xlsx")}"'
    workbook.save(response)
    return response


def _audit_export(request, product, fmt, row_count):
    """Record who exported what.

    Exports carry BVN, NIN, dates of birth and next-of-kin details when PII is
    enabled, so downloads must be attributable after the fact.
    """
    audit.info(
        "EXPORT format=%s product=%s user=%s ip=%s rows=%d pii=%s filters=%s",
        fmt,
        product,
        request.user.get_username(),
        request.META.get("REMOTE_ADDR", "?"),
        row_count,
        "included" if settings.EXPORT_INCLUDE_PII else "masked",
        request.GET.urlencode() or "(none)",
    )


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

# --- analytics -------------------------------------------------------------------

MONTH_RANGES = [6, 12, 24]
DEFAULT_MONTHS = 12

# --- presentation chart geometry ---------------------------------------------
# Each presentation page is one screen with no scrolling, and every chart is an
# SVG scaled to fit its card. The drawing sizes below are chosen to match the
# shape of the card they land in (see the layout CSS in analytics_panel.html and
# monthly_overview.html) so the SVG fills the card at close to 1:1 and its text
# stays readable. If a card's shape changes, change its numbers here with it.
PANEL_COLUMN = dict(width=680, height=330)                 # 2x2 grid: columns
PANEL_BAND = dict(width=680, row_height=46, label_width=130, value_width=70)
PANEL_STATE = dict(width=680, row_height=40, label_width=150, value_width=70)
# Cash for Car runs six cards, three across, so each is about half as wide as
# in the 2x2 grid. Drawing them narrower keeps their text near true size
# instead of scaled down to a squint.
PANEL_COLUMN_THIRD = dict(width=520, height=300)
PANEL_BAND_THIRD = dict(width=460, row_height=40, label_width=112, value_width=56)
PANEL_STATE_THIRD = dict(width=460, row_height=34, label_width=122, value_width=56)
PANEL_WINNER_THIRD = dict(width=460, row_height=64, label_width=130, value_width=56)
OVERVIEW_COLUMN = dict(width=680, height=330)              # wide top row
OVERVIEW_BAND = dict(width=460, row_height=36, label_width=112, value_width=56)
OVERVIEW_STATE = dict(width=460, row_height=28, label_width=122, value_width=56)
OVERVIEW_OFFICER = dict(width=460, row_height=32, label_width=176, value_width=84)
OVERVIEW_OFFICER_ROWS = 8

# STATUS_GROUPS/STATUS_GROUP_LABELS are imported from .status above - the
# single definition of what "In progress" means, shared with the drill-down
# filter in loans/filters.py.
#
# One colour per group, the same on every chart, so "Disbursed" is always
# green and "Rejected" always rose.
STATUS_GROUP_COLOURS = {
    "PENDING": "#94A3B8",
    "IN_PROGRESS": "#2563EB",
    "DISBURSED": "#15803D",
    "REJECTED": "#BE123C",
}


def _grouped_status_items(counts_by_code, drilldown_base_url=None):
    """Fold a {status code: count} dict down to the four "Final outcome" groups.

    When `drilldown_base_url` is given (the product list, or "All requests"
    for the overview), each group also gets a `url` to that same set of loan
    requests filtered down to it - what lets someone click "Rejected" on the
    pie and see exactly those requests, reasons included.
    """
    grouped = {key: 0 for key, _label in STATUS_GROUP_LABELS}
    for code, count in counts_by_code.items():
        grouped[STATUS_GROUPS.get(code, "IN_PROGRESS")] += count
    items = [
        {"label": label, "value": grouped[key], "colour": STATUS_GROUP_COLOURS[key]}
        for key, label in STATUS_GROUP_LABELS
    ]
    if drilldown_base_url:
        for item, (key, _label) in zip(items, STATUS_GROUP_LABELS):
            item["url"] = f"{drilldown_base_url}?status_group={key}"
    return items

@login_required
@handle_db_errors
def analytics(request):
    try:
        months = int(request.GET.get("months",DEFAULT_MONTHS))
    except (TypeError, ValueError):
        months = DEFAULT_MONTHS
    if months not in MONTH_RANGES:
        months = DEFAULT_MONTHS

    volume = volume_by_month(request.user, months)
    products = product_volume(request.user, months)
    bands = amount_bands(request.user)
    states = top_states(request.user)
    officers = officer_performance(request.user)
    times = turnaround(request.user)

    volume_chart = column_chart([{"label": m["label"], "value": m["total"]} for m in volume])
    value_chart = column_chart([{"label": m["label"], "value": m["value"]} for m in volume])
    product_chart = pie_chart(
        [{"label": p["label"], "value": p["count"]} for p in products]
    )
    band_chart = bar_chart(
        [{"label": b["label"], "value": b["count"]} for b in bands],
        label_width=130, value_width=70,
    )
    state_chart = bar_chart(
        [{"label": s["label"], "value": s["count"]} for s in states],
        label_width=160, value_width=70,
    )
    officer_chart = bar_chart(
        [
            {
                "label": o["label"],
                "value": o["value"],
                "display": money(o["value"]),
                "note": f"{o['count']} loans"
                        + (f" \u00b7 {o['variants']} spellings" if o["variants"] > 1 else ""),
            }
            for o in officers
        ],
        label_width=230, value_width=150,
    )

    presentation_mode = request.GET.get("presentation") == "1"

    return render(
        request,
        "loans/analytics.html",
        {
            "months": months,
            "month_ranges": MONTH_RANGES,
            "volume": volume,
            "volume_chart": volume_chart,
            "value_chart": value_chart,
            "products": products,
            "product_chart": product_chart,
            "band_chart": band_chart,
            "state_chart": state_chart,
            "officer_chart": officer_chart,
            "officers": officers,
            "turnaround": times,
            "total_applications": sum(m["total"] for m in volume),
            "total_value": sum(m["value"] for m in volume),
            "sample": settings.SAMPLE_DB,
            "product": "analytics",
            "presentation": presentation_mode,
        },
    )


@login_required
@handle_db_errors
def monthly_overview(request):
    """One compact, presentation-friendly screen: applications per month,
    values requested per month, loan size distribution, top states and
    officer performance - the same figures as the full Analytics page,
    trimmed to what fits one slide without scrolling. The full page (with
    the volume table, turnaround stats and officer table) stays at
    /analytics/ for normal browsing.
    """
    try:
        months = int(request.GET.get("months", DEFAULT_MONTHS))
    except (TypeError, ValueError):
        months = DEFAULT_MONTHS
    if months not in MONTH_RANGES:
        months = DEFAULT_MONTHS

    volume = volume_by_month(request.user, months)
    bands = amount_bands(request.user)
    states = top_states(request.user)
    officers = officer_performance(request.user)

    volume_chart = column_chart([{"label": m["label"], "value": m["total"]} for m in volume], **OVERVIEW_COLUMN)
    value_chart = column_chart([{"label": m["label"], "value": m["value"]} for m in volume], **OVERVIEW_COLUMN)
    band_chart = bar_chart(
        [{"label": b["label"], "value": b["count"]} for b in bands],
        **OVERVIEW_BAND,
    )
    state_chart = bar_chart(
        [{"label": s["label"], "value": s["count"]} for s in states],
        **OVERVIEW_STATE,
    )
    # Top few only: this is a card in a one-screen layout, not the full table
    # (the Analytics page has all of them).
    officer_chart = bar_chart(
        [
            {
                "label": o["label"],
                "value": o["value"],
                "display": "\u20a6" + compact_number(o["value"]),
                "note": f"{o['count']} loans"
                        + (f" \u00b7 {o['variants']} spellings" if o["variants"] > 1 else ""),
            }
            for o in officers[:OVERVIEW_OFFICER_ROWS]
        ],
        **OVERVIEW_OFFICER,
    )

    presentation_mode = request.GET.get("presentation") == "1"

    return render(
        request,
        "loans/monthly_overview.html",
        {
            "months": months,
            "month_ranges": MONTH_RANGES,
            "volume_chart": volume_chart,
            "value_chart": value_chart,
            "band_chart": band_chart,
            "state_chart": state_chart,
            "officer_chart": officer_chart,
            "total_applications": sum(m["total"] for m in volume),
            "total_value": sum(m["value"] for m in volume),
            "total_value_compact": compact_number(sum(m["value"] for m in volume)),
            "sample": settings.SAMPLE_DB,
            "product": "monthly-overview",
            "presentation": presentation_mode,
        },
    )


@login_required
@handle_db_errors
def analytics_panel(request):
    """Dashboard summary used for the presentation deck.

    Without a product filter it shows the cross-product overview. When
    ``?product=...`` is supplied it narrows the same dashboard to one loan type,
    which is used for the remaining presentation pages.
    """
    try:
        months = int(request.GET.get("months", DEFAULT_MONTHS))
    except (TypeError, ValueError):
        months = DEFAULT_MONTHS
    if months not in MONTH_RANGES:
        months = DEFAULT_MONTHS

    def status_summary(qs):
        totals = {code: 0 for code, _label in STATUS_CHOICES}
        for row in qs.values("status").annotate(n=Count("id")):
            totals[row["status"]] = totals.get(row["status"], 0) + row["n"]
        total = sum(totals.values())
        disbursed = totals.get("DISBURSED", 0)
        rejected = totals.get("REJECTED", 0)
        in_progress = total - disbursed - rejected
        return {
            "total": total,
            "submitted": total,
            "disbursed": disbursed,
            "in_progress": in_progress,
            "rejected": rejected,
            "statuses": totals,
        }

    def summary_cards(summary, drilldown_base_url=None):
        def url(status_group=None):
            if not drilldown_base_url:
                return None
            if status_group:
                return f"{drilldown_base_url}?status_group={status_group}"
            return drilldown_base_url

        return [
            {"label": "Applications", "value": summary["total"], "url": url(), "colour": None},
            {"label": "Submitted", "value": summary["submitted"], "url": url(), "colour": None},
            {"label": "Disbursed", "value": summary["disbursed"], "url": url("DISBURSED"), "colour": STATUS_GROUP_COLOURS["DISBURSED"]},
            {"label": "In progress", "value": summary["in_progress"], "url": url("IN_PROGRESS"), "colour": STATUS_GROUP_COLOURS["IN_PROGRESS"]},
            {"label": "Rejected", "value": summary["rejected"], "url": url("REJECTED"), "colour": STATUS_GROUP_COLOURS["REJECTED"]},
        ]

    product_code = (request.GET.get("product") or "").strip()
    selected_model = PRODUCTS.get(product_code)
    presentation_mode = request.GET.get("presentation") == "1"

    # Where a click on a status pie slice, legend row or KPI tile goes: the
    # product's own list, filtered, when one product is selected; "All
    # requests" filtered the same way on the overview.
    drilldown_base_url = (
        reverse("product-list", args=[product_code])
        if selected_model is not None
        else reverse("all-requests")
    )

    volume = volume_by_month(request.user, months)
    products = product_volume(request.user, months)
    commission_chart = None
    commission_winner_chart = None

    if selected_model is not None:
        product_rows = [row for row in products if row["code"] == product_code]
        summary = product_rows[0] if product_rows else {
            "label": selected_model.PRODUCT_LABEL,
            "code": product_code,
            "count": 0,
            "value": 0,
            "disbursed": 0,
        }
        qs = selected_model.objects.visible_to(request.user)
        summary_status = status_summary(qs)

        volume_points = [
            {"label": month["label"], "value": month["by_product"].get(product_code, 0)}
            for month in volume
        ]

        final_outcome_items = _grouped_status_items(summary_status["statuses"], drilldown_base_url)
        final_outcome_chart = pie_chart(final_outcome_items)
        approval_chart = bar_chart(
            [{"label": status_label(code), "value": summary_status["statuses"].get(code, 0)} for code, _label in STATUS_CHOICES],
            width=460,
            row_height=30,
            label_width=128,
            value_width=90,
        )

        band_definitions = [
            ("Under 250k", 0, 250_000),
            ("250k - 500k", 250_000, 500_000),
            ("500k - 1m", 500_000, 1_000_000),
            ("1m - 5m", 1_000_000, 5_000_000),
            ("5m - 20m", 5_000_000, 20_000_000),
            ("20m - 50m", 20_000_000, 50_000_000),
            ("Over 50m", 50_000_000, None),
        ]
        band_counts = {label: 0 for label, _low, _high in band_definitions}
        for row in qs.values("loan_amount").annotate(n=Count("id")):
            amount = float(row["loan_amount"] or 0)
            for label, low, high in band_definitions:
                if amount >= low and (high is None or amount < high):
                    band_counts[label] += row["n"]
        band_points = [{"label": label, "value": band_counts.get(label, 0)} for label, _low, _high in band_definitions]
        third = selected_model is CashForCarRequest
        band_chart = bar_chart(band_points, **(PANEL_BAND_THIRD if third else PANEL_BAND))

        state_totals = {}
        for row in qs.values("state").annotate(n=Count("id")):
            name = (row["state"] or "").strip() or "(not recorded)"
            state_totals[name.title()] = state_totals.get(name.title(), 0) + row["n"]
        state_items = [{"label": label, "value": value} for label, value in sorted(state_totals.items(), key=lambda item: -item[1])[:8]]
        state_chart = bar_chart(state_items, **(PANEL_STATE_THIRD if third else PANEL_STATE))

        # Cash for Car splits its fee between the two parties behind the
        # deal - Floauto (the partner) and Dash (us). Nothing else in the
        # portal has this, so the chart only appears on this one product.
        # Counted on disbursed loans only - a pending or rejected request
        # hasn't actually paid anyone anything yet.
        if selected_model is CashForCarRequest:
            commission_totals = qs.filter(status="DISBURSED").aggregate(
                floauto=Sum("fee_floauto_share"), dash=Sum("fee_dash_share")
            )
            commission_chart = pie_chart([
                {"label": "Dash", "value": float(commission_totals["dash"] or 0), "colour": SERIES_COLOURS[0]},
                {"label": "Floauto", "value": float(commission_totals["floauto"] or 0), "colour": SERIES_COLOURS[3]},
            ])

            # Same loans, compared one request at a time: on how many did
            # each party take the bigger cut? A missing share counts as 0.
            # The total above can be swung by a few large loans; this
            # shows who usually comes out ahead.
            zero = Value(0, output_field=DecimalField())
            per_request = (
                qs.filter(status="DISBURSED")
                .annotate(
                    _floauto=Coalesce("fee_floauto_share", zero),
                    _dash=Coalesce("fee_dash_share", zero),
                )
                .aggregate(
                    dash_higher=Count("pk", filter=Q(_dash__gt=F("_floauto"))),
                    floauto_higher=Count("pk", filter=Q(_floauto__gt=F("_dash"))),
                    equal=Count("pk", filter=Q(_floauto=F("_dash"))),
                )
            )
            commission_winner_chart = bar_chart(
                [
                    {"label": "Dash higher", "value": per_request["dash_higher"], "colour": SERIES_COLOURS[0]},
                    {"label": "Floauto higher", "value": per_request["floauto_higher"], "colour": SERIES_COLOURS[3]},
                    {"label": "Equal", "value": per_request["equal"], "colour": STATUS_GROUP_COLOURS["PENDING"]},
                ],
                **PANEL_WINNER_THIRD,
            )

        product_chart = pie_chart([{"label": summary["label"], "value": summary["count"], "url": drilldown_base_url}])
        volume_chart = column_chart(volume_points, **(PANEL_COLUMN_THIRD if third else PANEL_COLUMN))
        value_chart = column_chart([
            {"label": month["label"], "value": month["value"]}
            for month in volume
        ], **PANEL_COLUMN)
        loan_volume_chart = column_chart([
            {"label": summary["label"], "value": summary["value"]}
        ], **PANEL_COLUMN)
        per_products = [{
            "code": summary["code"],
            "label": summary["label"],
            "approval_chart": approval_chart,
            "loan_volume": summary["value"],
            "count": summary["count"],
            "status_cards": summary_cards(summary_status, drilldown_base_url),
        }]
        total_applications = summary["count"]
        total_value = summary["value"]
        product_title = selected_model.PRODUCT_LABEL
    else:
        product_chart = pie_chart([
            {"label": p["label"], "value": p["count"], "url": reverse("product-list", args=[p["code"]])}
            for p in products
        ])
        volume_chart = column_chart([{"label": m["label"], "value": m["total"]} for m in volume], **PANEL_COLUMN)
        value_chart = column_chart([{"label": m["label"], "value": m["value"]} for m in volume], **PANEL_COLUMN)
        # Same colour per product as the pie above it (both walk `products`
        # in the same order), so a viewer reads "purple = Cashback" once and
        # it holds across both charts.
        loan_volume_chart = column_chart(
            [
                {"label": p["label"], "value": p["value"], "colour": SERIES_COLOURS[i % len(SERIES_COLOURS)]}
                for i, p in enumerate(products)
            ],
            **PANEL_COLUMN,
        )

        final_outcome_items = []
        from .models import PRODUCTS as PRODUCT_MODELS
        aggregated = {code: 0 for code, _label in STATUS_CHOICES}
        for code, model in PRODUCT_MODELS.items():
            qs = model.objects.visible_to(request.user)
            for row in qs.values("status").annotate(n=Count("id")):
                aggregated[row["status"]] = aggregated.get(row["status"], 0) + row["n"]
        final_outcome_items = _grouped_status_items(aggregated, drilldown_base_url)
        final_outcome_chart = pie_chart(final_outcome_items)

        per_products = []
        for p in products:
            code = p["code"]
            label = p["label"]
            model = PRODUCT_MODELS[code]
            qs = model.objects.visible_to(request.user)
            counts = {c: 0 for c, _l in STATUS_CHOICES}
            for row in qs.values("status").annotate(n=Count("id")):
                counts[row["status"]] = counts.get(row["status"], 0) + row["n"]
            item_summary = {
                "total": sum(counts.values()),
                "submitted": sum(counts.values()),
                "disbursed": counts.get("DISBURSED", 0),
                "in_progress": sum(counts.values()) - counts.get("DISBURSED", 0) - counts.get("REJECTED", 0),
                "rejected": counts.get("REJECTED", 0),
            }
            items = [{"label": status_label(c), "value": counts.get(c, 0)} for c, _l in STATUS_CHOICES]
            approval_chart = bar_chart(items, width=360, row_height=28, label_width=120, value_width=90)
            per_products.append({
                "code": code,
                "label": label,
                "approval_chart": approval_chart,
                "loan_volume": p["value"],
                "count": p["count"],
                "status_cards": summary_cards(item_summary),
            })

        total_applications = sum(m["total"] for m in volume)
        total_value = sum(m["value"] for m in volume)
        product_title = "Analytics Panel"

    context = {
        "months": months,
        "month_ranges": MONTH_RANGES,
        "product_chart": product_chart,
        "final_outcome_chart": final_outcome_chart,
        "volume_chart": volume_chart,
        "value_chart": value_chart,
        "loan_volume_chart": loan_volume_chart,
        "per_products": per_products,
        "summary_cards": summary_cards(status_summary(selected_model.objects.visible_to(request.user)) if selected_model is not None else {
            "total": total_applications,
            "submitted": total_applications,
            "disbursed": sum(
                model.objects.visible_to(request.user).filter(status="DISBURSED").count() for model in PRODUCTS.values()
            ),
            "in_progress": total_applications - sum(
                model.objects.visible_to(request.user).filter(status="DISBURSED").count() for model in PRODUCTS.values()
            ) - sum(
                model.objects.visible_to(request.user).filter(status="REJECTED").count() for model in PRODUCTS.values()
            ),
            "rejected": sum(
                model.objects.visible_to(request.user).filter(status="REJECTED").count() for model in PRODUCTS.values()
            ),
        }, drilldown_base_url),
        "total_applications": total_applications,
        "total_value": total_value,
        "sample": settings.SAMPLE_DB,
        "presentation": presentation_mode,
        "product": "analytics-panel",
        "product_title": product_title,
        "selected_product": product_code,
        "band_chart": band_chart if selected_model is not None else None,
        "state_chart": state_chart if selected_model is not None else None,
        "commission_chart": commission_chart,
        "commission_winner_chart": commission_winner_chart,
    }
    return render(request, "loans/analytics_panel.html", context)


@login_required
@handle_db_errors
def presentation(request):
    slides = [
        {
            "type": "analytics-panel",
            "title": "Analytics Panel",
            "url": reverse("analytics-panel") + "?presentation=1",
        },
        {
            "type": "monthly-overview",
            "title": "Monthly Overview",
            "url": reverse("monthly-overview") + "?presentation=1",
        },
    ]
    for code, model in PRODUCTS.items():
        slides.append(
            {
                "type": "product",
                "title": model.PRODUCT_LABEL,
                "url": reverse("analytics-panel") + f"?presentation=1&product={code}",
            }
        )
    return render(
        request,
        "loans/presentation.html",
        {
            "slides": slides,
            "sample": settings.SAMPLE_DB,
            "presentation_mode": True,
        },
    )