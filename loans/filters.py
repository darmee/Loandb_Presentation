"""Filters for the loan request lists.

One filter set serves all three products. The only per-product difference is
the column holding the family name - `surname` on cashback, `last_name` on the
other two - so the search method reads `LAST_NAME_FIELD` from the model rather
than hard-coding it.

Status is filtered on the annotated `status` value (see loans/status.py), so a
status filter is a WHERE on the same CASE the list and the tiles display. The
alternative - filtering on the underlying booleans - would drift the moment the
precedence rules changed.
"""

import django_filters
from django import forms
from django.db.models import Q
from django.db.utils import Error as DatabaseError

from .status import STATUS_CHOICES, STATUS_GROUP_CHOICES, status_codes_in_group


def _distinct_choices(model, column):
    """Distinct values for `column`, as form choices.

    Read from the table rather than hard-coded, so a new state or account
    officer appears without a code change. Returns an empty list if the
    database is unreachable, so the page can still render its connectivity
    error instead of raising while the form is built.
    """

    def choices():
        try:
            values = (
                model.objects.order_by(column)
                .values_list(column, flat=True)
                .distinct()
            )
            return [(v, v) for v in values if v]
        except DatabaseError:
            return []

    return choices


class LoanRequestFilter(django_filters.FilterSet):
    q = django_filters.CharFilter(
        method="search",
        label="Search",
        widget=forms.TextInput(
            attrs={"placeholder": "Reference, name, BVN, NIN, phone, email..."}
        ),
    )

    status = django_filters.ChoiceFilter(
        choices=STATUS_CHOICES, empty_label="All statuses", label="Status"
    )

    # Additive, presentation-only: the "Final outcome" pie shows four groups
    # (Pending / In progress / Disbursed / Rejected), and "In progress" folds
    # together four different raw statuses that the plain `status` filter
    # above can't express in one value. Not exposed as a form field - a
    # drill-down link builds the query string directly (see
    # loans/views.py:_status_group_url) - so it never touches the existing
    # status dropdown or any link built before this was added.
    status_group = django_filters.CharFilter(method="filter_status_group", label="Status group")

    def filter_status_group(self, queryset, name, value):
        if value not in STATUS_GROUP_CHOICES:
            return queryset
        return queryset.filter(status__in=status_codes_in_group(value))

    created_from = django_filters.DateFilter(
        field_name="created_at",
        lookup_expr="date__gte",
        label="Created from",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    created_to = django_filters.DateFilter(
        field_name="created_at",
        lookup_expr="date__lte",
        label="Created to",
        widget=forms.DateInput(attrs={"type": "date"}),
    )

    amount_min = django_filters.NumberFilter(
        field_name="loan_amount", lookup_expr="gte", label="Amount from"
    )
    amount_max = django_filters.NumberFilter(
        field_name="loan_amount", lookup_expr="lte", label="Amount to"
    )

    account_officer = django_filters.CharFilter(
        lookup_expr="icontains", label="Account officer"
    )
    state = django_filters.CharFilter(lookup_expr="iexact", label="State")

    is_legacy = django_filters.BooleanFilter(
        label="Legacy record",
        widget=forms.Select(choices=[("", "All records"), ("false", "Current"), ("true", "Legacy")]),
    )

    # Column sorting. Rendered as clickable table headers rather than a select,
    # so it is excluded from the filter grid in the template.
    sort = django_filters.OrderingFilter(
        fields=(
            "created_at",
            "reference_no",
            "first_name",
            "loan_amount",
            "status",
            "account_officer",
        )
    )

    class Meta:
        fields = []

    def search(self, queryset, name, value):
        value = value.strip()
        if not value:
            return queryset

        last_name_field = self.Meta.model.LAST_NAME_FIELD
        terms = (
            Q(reference_no__icontains=value)
            | Q(first_name__icontains=value)
            | Q(**{f"{last_name_field}__icontains": value})
            | Q(email__icontains=value)
            | Q(phone__icontains=value)
            | Q(account_officer__icontains=value)
            | Q(loan_purpose__icontains=value)
        )
        # BVN and NIN are exact-match only. They are regulated identifiers and
        # a partial match would let someone probe for them a digit at a time.
        if value.isdigit():
            terms = terms | Q(bvn=value) | Q(nin=value)

        if hasattr(self.Meta.model, "middle_name"):
            terms = terms | Q(middle_name__icontains=value)

        return queryset.filter(terms)


def filter_for(model):
    """Build the filter set for one loan product."""
    return type(
        f"{model.__name__}Filter",
        (LoanRequestFilter,),
        {"Meta": type("Meta", (LoanRequestFilter.Meta,), {"model": model, "fields": []})},
    )
