"""Derived application status.

The loan tables have no `status` column. An application's state is implied by
five booleans on its approval spine, so this module derives it - once, in one
place, in two forms that cannot drift apart:

  * `status_expression()` builds a SQL CASE, so filtering, sorting, grouping
    and the dashboard totals all happen in the database.
  * `derive_status()` does the same thing in Python, for a single object.

Both are generated from STATUS_RULES below. That matters: if the list view
computed status one way and the stat tiles another, the tiles would stop
reconciling with the table underneath them, and the bug would look like a
database problem rather than a code one. Generating both from one list makes
that class of bug impossible, and `tests/test_status.py` proves the two agree
over every combination of flags.

The ORDER of STATUS_RULES is the business rule: the first flag that is set
wins. Reading it top to bottom - an application that has been rejected is
rejected whatever else happened to it; one that has been disbursed is done; an
open correction request is what currently blocks an application even though an
earlier stage may already have approved it; otherwise the furthest stage
reached is the status.
"""

from django.db.models import Case, CharField, Value, When

STATUS_RULES = [
    ("REJECTED", "rejected", "Rejected"),
    ("DISBURSED", "disbursed", "Disbursed"),
    ("CORRECTION_REQUESTED", "correction_requested", "Correction requested"),
    ("CONTROL_APPROVED", "control_approved", "Control approved"),
    ("CREDIT_APPROVED", "credit_approved", "Credit approved"),
    ("REVIEWED", "reviewed", "Reviewed"),
]

PENDING = "PENDING"
PENDING_LABEL = "Pending"

STATUS_CHOICES = [(code, label) for code, _field, label in STATUS_RULES] + [
    (PENDING, PENDING_LABEL)
]
STATUS_LABELS = dict(STATUS_CHOICES)

STATUS_FIELDS = [field for _code, field, _label in STATUS_RULES]

TERMINAL_STATUSES = ["REJECTED", "DISBURSED"]

OPEN_STATUSES = [code for code, _f, _l in STATUS_RULES if code not in TERMINAL_STATUSES] + [
    PENDING
]

PIPELINE_POSITION = {
    PENDING: 0,
    "REVIEWED": 1,
    "CREDIT_APPROVED": 2,
    "CONTROL_APPROVED": 3,
    "DISBURSED": 4,
}
PIPELINE_LENGTH = 4


def status_expression():
    """A SQL CASE deriving status, for use with .annotate(status=...).

    Built fresh on each call rather than defined as a module constant: query
    expressions carry state once attached to a queryset, so sharing one
    instance across queries is a subtle source of bugs.
    """
    return Case(
        *[
            When(**{field: True}, then=Value(code))
            for code, field, _label in STATUS_RULES
        ],
        default=Value(PENDING),
        output_field=CharField(),
    )


def derive_status(obj):
    """The same derivation as `status_expression()`, for a single instance."""
    for code, field, _label in STATUS_RULES:
        if getattr(obj, field, False):
            return code
    return PENDING


def status_label(code):
    """Human label for a status code, falling back to the code itself."""
    return STATUS_LABELS.get(code, code)


# The presentation's "Final outcome" pie (and its drill-down links) fold the
# four mid-pipeline statuses into one "In progress" wedge - Pending, In
# progress, Disbursed, Rejected is the story a viewer glancing at the deck
# needs, not all seven raw statuses. Defined here, next to STATUS_CHOICES, so
# the chart-building code and the filter that lets someone click through to
# the underlying rows can't drift apart on what "In progress" means.
STATUS_GROUPS = {
    "PENDING": "PENDING",
    "REVIEWED": "IN_PROGRESS",
    "CREDIT_APPROVED": "IN_PROGRESS",
    "CONTROL_APPROVED": "IN_PROGRESS",
    "CORRECTION_REQUESTED": "IN_PROGRESS",
    "DISBURSED": "DISBURSED",
    "REJECTED": "REJECTED",
}
STATUS_GROUP_LABELS = [
    ("PENDING", "Pending"),
    ("IN_PROGRESS", "In progress"),
    ("DISBURSED", "Disbursed"),
    ("REJECTED", "Rejected"),
]
STATUS_GROUP_CHOICES = {key for key, _label in STATUS_GROUP_LABELS}


def status_codes_in_group(group):
    """Every raw status code that folds into one "Final outcome" group."""
    return [code for code, g in STATUS_GROUPS.items() if g == group]
