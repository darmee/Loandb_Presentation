"""The loan journey: every analysis the presentation shows, in one place.

An application moves through five milestones, each recorded as a flag plus a
timestamp on the approval spine:

    Submitted -> Reviewed -> Credit approved -> Control approved -> Disbursed

Between each pair of milestones sits a *gate* - the queue of work that has to
happen to move on (Review, Credit, Control, Disbursement). An application can
leave the journey at any gate, either because it was rejected there or because
it is still sitting there. Corrections send an application back to the
applicant from a gate, and it may or may not come back.

Two ways of counting, and the presentation uses both on purpose:

* **Activity** ("what happened on 15 Sep?") counts each milestone on the day
  it happened, whenever the application was submitted. This is what the daily
  charts show.
* **Cohort** ("of what was submitted in September, how much got disbursed?")
  follows the applications submitted in the window to wherever they are now.
  Funnels, conversion and drop-off use this, because a conversion rate is only
  meaningful when the numerator is a subset of the denominator.

Everything here works on plain `Record` objects loaded once per request by
`load_records`, so the analyses are ordinary Python and are unit-testable
without a database. That is reasonable at the current size (a few thousand
applications). If the tables reach the hundreds of thousands, push the daily
aggregation into SQL first - it is the only part that touches every row.
"""

import re
from collections import Counter, defaultdict
from functools import lru_cache
from datetime import date, datetime, timedelta
from statistics import median

from django.db.models import F
from django.utils import timezone

from .models import PRODUCTS
from .officers import canonical as canonical_officer
from .status import status_label

# --- the journey -------------------------------------------------------------

# Milestones, in order. (key, label, flag column, timestamp column)
MILESTONES = [
    ("SUBMITTED", "Submitted", None, "created_at"),
    ("REVIEWED", "Reviewed", "reviewed", "reviewed_at"),
    ("CREDIT_APPROVED", "Credit approved", "credit_approved", "credit_approved_at"),
    ("CONTROL_APPROVED", "Control approved", "control_approved", "control_approved_at"),
    ("DISBURSED", "Disbursed", "disbursed", "disbursed_at"),
]
MILESTONE_KEYS = [key for key, *_ in MILESTONES]
MILESTONE_LABELS = {key: label for key, label, *_ in MILESTONES}

# Gate i sits between milestone i and milestone i+1.
GATES = ["REVIEW", "CREDIT", "CONTROL", "DISBURSEMENT"]
GATE_LABELS = {
    "REVIEW": "Review",
    "CREDIT": "Credit approval",
    "CONTROL": "Internal control",
    "DISBURSEMENT": "Disbursement",
}

# Events counted per day on the activity charts.
EVENTS = [
    ("submitted", "Submitted"),
    ("reviewed", "Reviewed"),
    ("credit_approved", "Credit approved"),
    ("control_approved", "Control approved"),
    ("disbursed", "Disbursed"),
    ("rejected", "Rejected"),
    ("correction", "Correction requested"),
]
EVENT_LABELS = dict(EVENTS)

# The five steps the daily report's day-by-day table counts and times:
# "Approved" there is control approval, the last sign-off before money moves.
REPORT_EVENTS = frozenset({"submitted", "reviewed", "control_approved", "disbursed", "rejected"})

# Waiting longer than this at a gate counts as "stalled" rather than "in queue".
STALLED_DAYS = 7

AGE_BUCKETS = [
    ("< 1 day", 1),
    ("1-3 days", 3),
    ("3-7 days", 7),
    ("7-14 days", 14),
    ("14-30 days", 30),
    ("30+ days", None),
]


def normalise_gate(raw, fallback=None):
    """Map whatever the origination system wrote in *_stage onto a gate.

    The column is free text (`character varying(40)`), so match on substrings
    rather than exact values: "CREDIT", "credit_approval" and
    "CREDIT_SUPERVISOR" all mean the credit gate.
    """
    value = (raw or "").upper()
    if "REVIEW" in value:
        return "REVIEW"
    if "CREDIT" in value:
        return "CREDIT"
    if "CONTROL" in value:
        return "CONTROL"
    if "DISBURS" in value or "OPERATION" in value:
        return "DISBURSEMENT"
    return fallback


# --- free-text categorisation ------------------------------------------------

# Rejection reasons and correction messages are free text. Exact strings are
# shown as they are, and also folded into broad categories so that
# "Affordability below threshold." and "DTI too high" count together. First
# match wins, so order matters. Extend these lists as real reasons appear.
REJECTION_CATEGORIES = [
    ("Applicant withdrew", r"withdr|cancel|no longer|not interested|declined offer"),
    ("Fraud / inconsistency", r"fraud|inconsisten|forg|fake|suspicious|tamper"),
    ("Duplicate / existing loan", r"duplicate|existing loan|already has|running loan|multiple"),
    ("Affordability", r"afford|income|debt.to.income|\bdti\b|repayment capacity|salary too low|threshold"),
    ("Credit history", r"bureau|credit history|adverse|delinquen|default|\bcrc\b|first central"),
    ("Identity / KYC", r"\bbvn\b|\bnin\b|identity|\bkyc\b|\bid\b|mismatch"),
    ("Employment", r"employ|employer|\bjob\b|ippis"),
    ("Guarantor / security", r"guarantor|collateral|security|valuation|vehicle"),
    ("Policy / eligibility", r"policy|eligib|\bage\b|outside|criteria|not qualif"),
    ("Documentation", r"document|statement|incomplete|illegible|missing|utility|payslip"),
    ("Account details", r"account|bank detail"),
]
CORRECTION_CATEGORIES = [
    ("Bank statement", r"statement"),
    ("Identity document", r"\bid\b|identity|passport|licen[cs]e|\bnin\b|\bbvn\b|government"),
    ("Proof of address", r"utility|address|proof of res"),
    ("Employment / income", r"employ|payslip|salary|income|letter"),
    ("Name / detail mismatch", r"match|name|incorrect|wrong"),
    ("Signature / consent", r"signature|sign|consent|agreement"),
]

_COMPILED = {}


def categorise(text, categories):
    if not text or not text.strip():
        return "Not recorded"
    for label, pattern in categories:
        compiled = _COMPILED.get(pattern)
        if compiled is None:
            compiled = _COMPILED[pattern] = re.compile(pattern, re.I)
        if compiled.search(text):
            return label
    return "Other"


def _text_key(text):
    """Group free text that differs only in case, spacing or punctuation."""
    if not text or not text.strip():
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower())).strip()


# --- records -----------------------------------------------------------------

LOAD_FIELDS = [
    "id", "reference_no", "first_name", "loan_amount", "account_officer", "state",
    "created_at", "reviewed", "reviewed_at", "credit_approved", "credit_approved_at",
    "control_approved", "control_approved_at", "disbursed", "disbursed_at",
    "rejected", "rejected_at", "rejection_reason", "rejected_stage",
    "correction_requested", "correction_requested_at", "correction_count",
    "correction_stage", "correction_message", "status",
]


class Record:
    """One application, reduced to what the journey analyses need."""

    __slots__ = (
        "product", "product_label", "id", "reference", "name", "amount", "officer",
        "state", "status", "at", "furthest", "rejected", "rejected_at",
        "rejection_reason", "rejected_gate", "rejected_stage_raw", "correction_open", "correction_at",
        "correction_count", "correction_gate", "correction_message",
    )

    def __init__(self, product, product_label, row):
        self.product = product
        self.product_label = product_label
        self.id = row["id"]
        self.reference = row["reference_no"]
        self.name = " ".join(p for p in (row["first_name"], row.get("last_name")) if p)
        self.amount = float(row["loan_amount"] or 0)
        self.officer = canonical_officer(row["account_officer"])
        self.state = ((row["state"] or "").strip() or "(not recorded)").title()
        self.status = row["status"]

        # Milestone timestamps. A flag that is set without a timestamp (legacy
        # rows) still counts as reached, it just cannot be placed on a day.
        self.at = {"SUBMITTED": row["created_at"]}
        furthest = 0
        for index, (key, _label, flag, column) in enumerate(MILESTONES[1:], start=1):
            if row[flag]:
                furthest = index
            self.at[key] = row[column] if row[flag] else None
        self.furthest = furthest

        self.rejected = bool(row["rejected"])
        self.rejected_at = row["rejected_at"] if self.rejected else None
        self.rejection_reason = (row["rejection_reason"] or "").strip()
        # The gate an application could not get past is the one after the
        # furthest milestone it reached. That is what the funnel counts, so it
        # is also what the rejection breakdown uses - the free-text
        # `rejected_stage` only decides it when every milestone is set.
        fallback = GATES[min(furthest, len(GATES) - 1)]
        if self.rejected:
            self.rejected_gate = (
                fallback if furthest < len(GATES) else normalise_gate(row["rejected_stage"], fallback)
            )
        else:
            self.rejected_gate = None
        self.rejected_stage_raw = (row["rejected_stage"] or "").strip()

        self.correction_open = self.status == "CORRECTION_REQUESTED"
        self.correction_at = row["correction_requested_at"]
        self.correction_count = row["correction_count"] or 0
        if self.correction_open and not self.correction_count:
            self.correction_count = 1
        self.correction_gate = (
            normalise_gate(row["correction_stage"], fallback)
            if (self.correction_count or self.correction_at) else None
        )
        self.correction_message = (row["correction_message"] or "").strip()

    # -- derived ------------------------------------------------------------

    @property
    def disbursed(self):
        # Rejected outranks disbursed, as in STATUS_RULES.
        return self.furthest == len(MILESTONES) - 1 and not self.rejected

    @property
    def closed(self):
        return self.disbursed or self.rejected

    @property
    def current_gate(self):
        """The gate an open application is waiting at; None once closed."""
        if self.closed:
            return None
        return GATES[self.furthest]

    def waiting_since(self):
        """When the application arrived at the gate it is waiting at."""
        stamps = [self.at[MILESTONE_KEYS[self.furthest]]]
        if self.correction_open and self.correction_at:
            stamps.append(self.correction_at)
        stamps = [s for s in stamps if s]
        return max(stamps) if stamps else self.at["SUBMITTED"]

    def lost_gate(self):
        """Where this application left the journey, if it has."""
        if self.rejected:
            return self.rejected_gate
        return None

    def summary(self):
        return {
            "product": self.product,
            "product_label": self.product_label,
            "id": self.id,
            "reference": self.reference,
            "name": self.name,
            "amount": self.amount,
            "officer": self.officer,
            "status": self.status,
            "status_label": status_label(self.status),
            "submitted": _iso(self.at["SUBMITTED"]),
        }


def load_records(user, product=None):
    """Every visible application, as Records. One query per product."""
    records = []
    for code, model in PRODUCTS.items():
        if product and product != code:
            continue
        rows = (
            model.objects.visible_to(user)
            .annotate(last_name_value=F(model.LAST_NAME_FIELD))
            .values(*LOAD_FIELDS, "last_name_value")
        )
        for row in rows:
            row["last_name"] = row.pop("last_name_value")
            records.append(Record(code, model.PRODUCT_LABEL, row))
    return records


# --- time helpers ------------------------------------------------------------

@lru_cache(maxsize=262144)
def local_date(value):
    # Cached: every analysis asks for the Lagos day of the same few thousand
    # timestamps, and the timezone conversion dominates the request otherwise.
    if value is None:
        return None
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.date()
    return value


@lru_cache(maxsize=262144)
def local_clock(value):
    """The Lagos time of day, "HH:MM", a timestamp fell at; None for a bare date."""
    if not isinstance(value, datetime):
        return None
    if timezone.is_aware(value):
        value = timezone.localtime(value)
    return value.strftime("%H:%M")


def _iso(value):
    if value is None:
        return None
    if isinstance(value, datetime) and timezone.is_aware(value):
        value = timezone.localtime(value)
    return value.isoformat()


def _between(value, start, end):
    day = local_date(value)
    return day is not None and start <= day <= end


def _days(delta):
    return delta.total_seconds() / 86400


def _median(values):
    return round(median(values), 1) if values else None


def _p90(values):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))], 1)


def _pct(part, whole):
    return round(part * 100 / whole, 1) if whole else None


def date_range(start, end):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def previous_window(start, end):
    """The window of equal length immediately before [start, end]."""
    length = (end - start).days + 1
    return start - timedelta(days=length), start - timedelta(days=1)


def data_span(records):
    """First and last submission date across everything loaded."""
    days = [local_date(r.at["SUBMITTED"]) for r in records if r.at["SUBMITTED"]]
    return {
        "total": len(records),
        "first": min(days).isoformat() if days else None,
        "last": max(days).isoformat() if days else None,
    }


def cohort(records, start, end):
    return [r for r in records if _between(r.at["SUBMITTED"], start, end)]


# --- daily activity ----------------------------------------------------------

def record_events(record):
    """(event, timestamp) pairs for everything that happened to an application."""
    events = [("submitted", record.at["SUBMITTED"])]
    for key, event in (("REVIEWED", "reviewed"), ("CREDIT_APPROVED", "credit_approved"),
                       ("CONTROL_APPROVED", "control_approved"), ("DISBURSED", "disbursed")):
        if record.at[key]:
            events.append((event, record.at[key]))
    if record.rejected_at:
        events.append(("rejected", record.rejected_at))
    if record.correction_at:
        events.append(("correction", record.correction_at))
    return events


def daily_activity(records, start, end):
    """One row per day: how many applications hit each milestone that day.

    `first_time` and `last_time` are the Lagos clock times ("HH:MM") of the
    day's first and last event among the steps the daily report shows, or
    None on a day with none - the hours the day was active.
    """
    blank = {event: 0 for event, _label in EVENTS}
    days = {day: dict(blank, submitted_value=0.0, disbursed_value=0.0, first_time=None, last_time=None)
            for day in date_range(start, end)}
    for record in records:
        for event, stamp in record_events(record):
            day = local_date(stamp)
            if day in days:
                row = days[day]
                row[event] += 1
                if event == "submitted":
                    row["submitted_value"] += record.amount
                elif event == "disbursed":
                    row["disbursed_value"] += record.amount
                if event in REPORT_EVENTS:
                    clock = local_clock(stamp)
                    if clock is not None:
                        if row["first_time"] is None or clock < row["first_time"]:
                            row["first_time"] = clock
                        if row["last_time"] is None or clock > row["last_time"]:
                            row["last_time"] = clock
    return [{"date": day.isoformat(), **values} for day, values in days.items()]


def day_detail(records, day):
    """Everything that happened on one day, for the drill-down drawer."""
    rows = []
    by_product = defaultdict(Counter)
    hours = defaultdict(Counter)
    totals = Counter()
    values = Counter()
    for record in records:
        for event, stamp in record_events(record):
            if local_date(stamp) != day:
                continue
            local = timezone.localtime(stamp) if timezone.is_aware(stamp) else stamp
            totals[event] += 1
            by_product[record.product_label][event] += 1
            hours[event][local.hour] += 1
            if event in ("submitted", "disbursed"):
                values[event] += record.amount
            row = record.summary()
            row.update({
                "event": event,
                "event_label": EVENT_LABELS[event],
                "time": local.strftime("%H:%M"),
                "detail": (
                    record.rejection_reason if event == "rejected"
                    else record.correction_message if event == "correction"
                    else ""
                ),
            })
            rows.append(row)
    rows.sort(key=lambda r: (r["time"], r["reference"]))
    return {
        "date": day.isoformat(),
        "totals": {event: totals.get(event, 0) for event, _label in EVENTS},
        "values": {"submitted": values["submitted"], "disbursed": values["disbursed"]},
        "by_product": [
            {"product": label, **{event: counts.get(event, 0) for event, _l in EVENTS}}
            for label, counts in sorted(by_product.items())
        ],
        "hourly": {event: [hours[event].get(h, 0) for h in range(24)] for event, _l in EVENTS},
        "rows": rows,
    }


# --- funnel, conversion, drop-off --------------------------------------------

def funnel(group):
    """How far the applications in a cohort have got."""
    total = len(group)
    reached = [sum(1 for r in group if r.furthest >= index) for index in range(len(MILESTONES))]
    stages = []
    for index, (key, label, *_rest) in enumerate(MILESTONES):
        count = reached[index]
        previous = reached[index - 1] if index else count
        stages.append({
            "key": key,
            "label": label,
            "count": count,
            "pct_of_submitted": _pct(count, total),
            "pct_of_previous": _pct(count, previous) if index else (100.0 if total else None),
            "lost_from_previous": previous - count if index else 0,
        })
    return stages


def gates(group, now=None):
    """Drop-off and bottleneck figures for each gate.

    For each gate: how many applications arrived, how many got through, how
    many were rejected there, and how many are still waiting there (split
    into "with the team" and "with the applicant" for open corrections). Time
    to clear the gate is measured on the applications that did.
    """
    now = now or timezone.now()
    out = []
    for index, gate in enumerate(GATES):
        arrived = [r for r in group if r.furthest >= index]
        passed = [r for r in arrived if r.furthest >= index + 1]
        rejected = [r for r in arrived if r.rejected and r.rejected_gate == gate]
        waiting = [r for r in arrived if r.current_gate == gate]
        with_applicant = [r for r in waiting if r.correction_open]
        ages = [_days(now - r.waiting_since()) for r in waiting if r.waiting_since()]
        stalled = sum(1 for age in ages if age >= STALLED_DAYS)

        start_key, end_key = MILESTONE_KEYS[index], MILESTONE_KEYS[index + 1]
        durations = [
            _days(r.at[end_key] - r.at[start_key])
            for r in passed
            if r.at[end_key] and r.at[start_key] and r.at[end_key] >= r.at[start_key]
        ]
        buckets = Counter()
        for age in ages:
            for label, ceiling in AGE_BUCKETS:
                if ceiling is None or age < ceiling:
                    buckets[label] += 1
                    break
        lost = len(rejected) + stalled
        out.append({
            "gate": gate,
            "label": GATE_LABELS[gate],
            "from": MILESTONE_LABELS[start_key],
            "to": MILESTONE_LABELS[end_key],
            "arrived": len(arrived),
            "passed": len(passed),
            "rejected": len(rejected),
            "waiting": len(waiting),
            "waiting_with_team": len(waiting) - len(with_applicant),
            "waiting_with_applicant": len(with_applicant),
            "stalled": stalled,
            "lost": lost,
            "pass_rate": _pct(len(passed), len(arrived)),
            "rejection_rate": _pct(len(rejected), len(arrived)),
            "loss_rate": _pct(lost, len(arrived)),
            "median_days": _median(durations),
            "p90_days": _p90(durations),
            "median_wait_days": _median(ages),
            "oldest_wait_days": round(max(ages), 1) if ages else None,
            "age_buckets": [{"label": label, "count": buckets.get(label, 0)} for label, _c in AGE_BUCKETS],
        })

    # The bottleneck is where the most work is piling up, weighted by how long
    # it has been there: a queue of 40 that clears in a day matters less than
    # a queue of 15 that has sat for two weeks.
    def pressure(g):
        return (g["waiting_with_team"] or 0) * (g["median_wait_days"] or 0) + (g["stalled"] or 0)

    if out:
        worst_loss = max(out, key=lambda g: (g["lost"], g["loss_rate"] or 0))
        slowest = max(out, key=lambda g: g["median_days"] or 0)
        pressured = max(out, key=pressure)
        for g in out:
            g["is_biggest_dropoff"] = g is worst_loss and g["lost"] > 0
            g["is_slowest"] = g is slowest and (g["median_days"] or 0) > 0
            g["is_bottleneck"] = g is pressured and pressure(g) > 0
            g["pressure"] = round(pressure(g), 1)
    return out


def outcome_summary(group):
    total = len(group)
    disbursed = [r for r in group if r.disbursed]
    rejected = [r for r in group if r.rejected]
    open_ = [r for r in group if not r.closed]
    to_disburse = [
        _days(r.at["DISBURSED"] - r.at["SUBMITTED"])
        for r in disbursed if r.at["DISBURSED"] and r.at["SUBMITTED"]
    ]
    decided = len(disbursed) + len(rejected)
    return {
        "submitted": total,
        "submitted_value": sum(r.amount for r in group),
        "disbursed": len(disbursed),
        "disbursed_value": sum(r.amount for r in disbursed),
        "rejected": len(rejected),
        "in_progress": len(open_),
        "correction_open": sum(1 for r in open_ if r.correction_open),
        "completion_rate": _pct(len(disbursed), total),
        "approval_rate": _pct(len(disbursed), decided),
        "rejection_rate": _pct(len(rejected), total),
        "median_days_to_disburse": _median(to_disburse),
    }


# --- applications vs disbursements -------------------------------------------

def flow(records, start, end):
    """Daily inflow (submitted) against outflow (disbursed + rejected).

    The running backlog is the cumulative difference within the window: if
    it climbs, the team is taking in work faster than it is closing it.
    """
    days = daily_activity(records, start, end)
    backlog = 0
    cumulative_in = cumulative_out = 0
    for row in days:
        cumulative_in += row["submitted"]
        cumulative_out += row["disbursed"]
        backlog += row["submitted"] - row["disbursed"] - row["rejected"]
        row["cumulative_submitted"] = cumulative_in
        row["cumulative_disbursed"] = cumulative_out
        row["backlog_change"] = backlog
    open_at_end = sum(
        1 for r in records
        if local_date(r.at["SUBMITTED"]) and local_date(r.at["SUBMITTED"]) <= end
        and not _closed_by(r, end)
    )
    total_in = sum(d["submitted"] for d in days)
    total_out = sum(d["disbursed"] for d in days)
    return {
        "days": days,
        "total_submitted": total_in,
        "total_disbursed": total_out,
        "total_rejected": sum(d["rejected"] for d in days),
        "submitted_value": sum(d["submitted_value"] for d in days),
        "disbursed_value": sum(d["disbursed_value"] for d in days),
        "disbursed_per_submitted": round(total_out / total_in, 2) if total_in else None,
        "open_at_end": open_at_end,
        "net_backlog_change": backlog,
    }


def _closed_by(record, day):
    stamps = [record.at["DISBURSED"], record.rejected_at]
    if record.disbursed and not record.at["DISBURSED"]:
        return True
    if record.rejected and not record.rejected_at:
        return True
    return any(s and local_date(s) <= day for s in stamps)


# --- rejections --------------------------------------------------------------

def rejections(records, start, end):
    """Applications rejected within the window, and why."""
    group = [
        r for r in records
        if r.rejected and _between(r.rejected_at or r.at["SUBMITTED"], start, end)
    ]
    decided = sum(
        1 for r in records
        if (r.disbursed and _between(r.at["DISBURSED"], start, end))
    ) + len(group)

    categories = Counter(categorise(r.rejection_reason, REJECTION_CATEGORIES) for r in group)
    reasons = defaultdict(lambda: {"count": 0, "value": 0.0, "label": None})
    for r in group:
        key = _text_key(r.rejection_reason)
        bucket = reasons[key]
        bucket["count"] += 1
        bucket["value"] += r.amount
        bucket["label"] = bucket["label"] or r.rejection_reason or "(no reason recorded)"
        bucket["category"] = categorise(r.rejection_reason, REJECTION_CATEGORIES)
    stage_counts = Counter(r.rejected_gate for r in group)
    product_counts = Counter(r.product_label for r in group)
    officer_counts = Counter(r.officer for r in group)
    stage_by_category = defaultdict(Counter)
    for r in group:
        stage_by_category[r.rejected_gate][categorise(r.rejection_reason, REJECTION_CATEGORIES)] += 1

    days_to_reject = [
        _days(r.rejected_at - r.at["SUBMITTED"]) for r in group if r.rejected_at and r.at["SUBMITTED"]
    ]
    daily = Counter(local_date(r.rejected_at) for r in group if r.rejected_at)

    total = len(group)
    return {
        "total": total,
        "value": sum(r.amount for r in group),
        "rejection_rate_of_decided": _pct(total, decided),
        "median_days_to_reject": _median(days_to_reject),
        "categories": [
            {"key": label, "label": label, "count": n, "pct": _pct(n, total)}
            for label, n in categories.most_common()
        ],
        "reasons": [
            {"key": key, "label": b["label"], "category": b["category"], "count": b["count"],
             "value": b["value"], "pct": _pct(b["count"], total)}
            for key, b in sorted(reasons.items(), key=lambda kv: -kv[1]["count"])[:15]
        ],
        "by_stage": [
            {"key": gate, "label": GATE_LABELS[gate], "count": stage_counts.get(gate, 0),
             "pct": _pct(stage_counts.get(gate, 0), total),
             "categories": dict(stage_by_category.get(gate, {}))}
            for gate in GATES
        ],
        "by_product": [{"key": k, "label": k, "count": n} for k, n in product_counts.most_common()],
        "by_officer": [{"key": k, "label": k, "count": n} for k, n in officer_counts.most_common(10)],
        "daily": [{"date": d.isoformat(), "count": daily.get(d, 0)} for d in date_range(start, end)],
    }


# --- corrections -------------------------------------------------------------

def corrections(records, start, end, now=None):
    """How often applications are sent back, why, and what it costs."""
    now = now or timezone.now()
    group = cohort(records, start, end)
    corrected = [r for r in group if r.correction_count]
    clean = [r for r in group if not r.correction_count]
    open_now = [r for r in records if r.correction_open]

    def outcome(rows):
        to_disburse = [
            _days(r.at["DISBURSED"] - r.at["SUBMITTED"])
            for r in rows if r.disbursed and r.at["DISBURSED"] and r.at["SUBMITTED"]
        ]
        return {
            "count": len(rows),
            "disbursed_pct": _pct(sum(1 for r in rows if r.disbursed), len(rows)),
            "rejected_pct": _pct(sum(1 for r in rows if r.rejected), len(rows)),
            "open_pct": _pct(sum(1 for r in rows if not r.closed), len(rows)),
            "median_days_to_disburse": _median(to_disburse),
        }

    rounds = Counter(min(r.correction_count, 3) for r in corrected)
    categories = Counter(categorise(r.correction_message, CORRECTION_CATEGORIES) for r in corrected)
    messages = defaultdict(lambda: {"count": 0, "label": None})
    for r in corrected:
        bucket = messages[_text_key(r.correction_message)]
        bucket["count"] += 1
        bucket["label"] = bucket["label"] or r.correction_message or "(no message recorded)"
        bucket["category"] = categorise(r.correction_message, CORRECTION_CATEGORIES)
    stages = Counter(r.correction_gate for r in corrected)
    open_ages = [_days(now - r.correction_at) for r in open_now if r.correction_at]
    buckets = Counter()
    for age in open_ages:
        for label, ceiling in AGE_BUCKETS:
            if ceiling is None or age < ceiling:
                buckets[label] += 1
                break
    daily = Counter(local_date(r.correction_at) for r in records if r.correction_at)

    total = len(corrected)
    return {
        "cohort": len(group),
        "corrected": total,
        "corrected_pct": _pct(total, len(group)),
        "rounds_total": sum(r.correction_count for r in corrected),
        "avg_rounds": round(sum(r.correction_count for r in corrected) / total, 2) if total else None,
        "open_now": len(open_now),
        "open_median_days": _median(open_ages),
        "open_oldest_days": round(max(open_ages), 1) if open_ages else None,
        "open_age_buckets": [{"label": l, "count": buckets.get(l, 0)} for l, _c in AGE_BUCKETS],
        "rounds": [
            {"key": str(n), "label": "3+ rounds" if n == 3 else f"{n} round{'s' if n > 1 else ''}",
             "count": rounds.get(n, 0)}
            for n in (1, 2, 3)
        ],
        "by_stage": [
            {"key": g, "label": GATE_LABELS[g], "count": stages.get(g, 0), "pct": _pct(stages.get(g, 0), total)}
            for g in GATES
        ],
        "categories": [
            {"key": label, "label": label, "count": n, "pct": _pct(n, total)}
            for label, n in categories.most_common()
        ],
        "messages": [
            {"key": key, "label": b["label"], "category": b["category"], "count": b["count"],
             "pct": _pct(b["count"], total)}
            for key, b in sorted(messages.items(), key=lambda kv: -kv[1]["count"])[:12]
        ],
        "outcome_corrected": outcome(corrected),
        "outcome_clean": outcome(clean),
        "daily": [{"date": d.isoformat(), "count": daily.get(d, 0)} for d in date_range(start, end)],
    }


# --- product split -----------------------------------------------------------

def by_product(group):
    rows = []
    for code, model in PRODUCTS.items():
        subset = [r for r in group if r.product == code]
        summary = outcome_summary(subset)
        summary.update({"key": code, "label": model.PRODUCT_LABEL})
        rows.append(summary)
    return rows


# --- period comparison -------------------------------------------------------

# A period is the applications submitted in it, followed to today: how many
# came in, how many of those have been paid out and for how much, how many were
# turned down. Every figure counts the same applications, so they add up with
# each other, and they are the figures the Overview, Monthly overview and
# request pages show for the same dates.
COMPARE_METRICS = [
    # key, label, kind, higher is better?
    ("submitted", "Applications", "count", True),
    ("disbursed", "Loans disbursed", "count", True),
    ("disbursed_value", "Amount disbursed", "money", True),
    ("rejected", "Rejected", "count", False),
    ("completion_rate", "Share paid out", "pct", True),
    ("median_days_to_disburse", "Days to disburse", "days", False),
]


def period_metrics(records, start, end):
    """One period's figures, and the same split day by day of application."""
    group = cohort(records, start, end)
    outcome = outcome_summary(group)
    metrics = {key: outcome[key] for key, *_rest in COMPARE_METRICS}
    return metrics, cohort_days(group, start, end)


def cohort_days(group, start, end):
    """For each day, the applications submitted on it and what became of them."""
    days = {day: {"submitted": 0, "disbursed": 0, "disbursed_value": 0.0, "rejected": 0}
            for day in date_range(start, end)}
    for r in group:
        row = days.get(local_date(r.at["SUBMITTED"]))
        if row is None:
            continue
        row["submitted"] += 1
        if r.disbursed:
            row["disbursed"] += 1
            row["disbursed_value"] += r.amount
        if r.rejected:
            row["rejected"] += 1
    return [{"date": day.isoformat(), **values} for day, values in days.items()]


def compare(records, a, b):
    metrics_a, days_a = period_metrics(records, *a)
    metrics_b, days_b = period_metrics(records, *b)
    rows = []
    for key, label, kind, higher_better in COMPARE_METRICS:
        va, vb = metrics_a.get(key), metrics_b.get(key)
        change = None
        if va is not None and vb is not None:
            if kind == "pct":
                change = round(va - vb, 1)          # percentage points
            elif vb:
                change = round((va - vb) * 100 / vb, 1)
        direction = None
        if change:
            direction = "good" if (change > 0) == higher_better else "bad"
        rows.append({"key": key, "label": label, "kind": kind, "a": va, "b": vb,
                     "change": change, "direction": direction})
    return {
        "a": {"start": a[0].isoformat(), "end": a[1].isoformat(), "days": days_a,
              "funnel": funnel(cohort(records, *a))},
        "b": {"start": b[0].isoformat(), "end": b[1].isoformat(), "days": days_b,
              "funnel": funnel(cohort(records, *b))},
        "metrics": rows,
    }


# --- top insights ------------------------------------------------------------

def _fmt_day(day_iso):
    return date.fromisoformat(day_iso).strftime("%a %d %b")


def _drill(label, kind, key=""):
    """A button that lists the applications a figure was counted from."""
    return {"label": label, "drill": {"kind": kind, "key": key}}


def _plural(count, word):
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def insights(records, start, end, now=None):
    """Findings in plain words, most important first.

    Each one carries:

    * `title` - what stands out, as a sentence anyone in the room follows;
    * `detail` - what it means, with a figure or two and no more;
    * `why` - how it was worked out, so nobody has to take it on trust;
    * `actions` - buttons to the evidence: the very applications the figures
      were counted from (a drill-down whose list is as long as the card says),
      the day in question, the two periods side by side, or the page with the
      chart. At most two, the strongest evidence first.
    """
    now = now or timezone.now()
    found = []
    group = cohort(records, start, end)
    prev_start, prev_end = previous_window(start, end)
    prev_group = cohort(records, prev_start, prev_end)
    outcome = outcome_summary(group)
    prev_outcome = outcome_summary(prev_group)
    gate_rows = gates(group, now)

    def add(score, tone, title, detail, why, actions):
        found.append({"score": score, "tone": tone, "title": title, "detail": detail, "why": why,
                      "actions": [a for a in actions if a][:2]})

    if not group:
        add(100, "neutral", "No applications in this period",
            "Nothing was submitted between these dates.",
            "Choose a wider date range at the top of the page.", [])
        return [_strip(i) for i in found]

    # Volume trend.
    if prev_group:
        change = (len(group) - len(prev_group)) * 100 / len(prev_group)
        if abs(change) >= 5:
            more = change > 0
            add(60 + min(abs(change), 40) / 2, "good" if more else "bad",
                f"{'More' if more else 'Fewer'} people applied than in the period before",
                f"{len(group):,} applications, against {len(prev_group):,} before: "
                f"{abs(change):.0f}% {'more' if more else 'fewer'}.",
                "Applications are counted on the day they were submitted, and set against "
                "the same number of days just before this period.",
                [{"label": "Compare the two periods", "compare": {
                    "a": [start.isoformat(), end.isoformat()],
                    "b": [prev_start.isoformat(), prev_end.isoformat()]}},
                 _drill(f"See the {_plural(len(group), 'application')}", "all")])

    # Biggest drop-off.
    lossy = max(gate_rows, key=lambda g: g["lost"])
    if lossy["lost"]:
        add(90, "bad", f"Most applications fall through at {lossy['label']}",
            f"Of the {lossy['arrived']:,} that reached it, {lossy['lost']:,} "
            f"{'was' if lossy['lost'] == 1 else 'were'} turned down there "
            f"or {'has' if lossy['lost'] == 1 else 'have'} sat there for over {STALLED_DAYS} days.",
            "Every step from review to disbursement is checked for how many applications it "
            "turns down or leaves waiting; this step loses the most.",
            [_drill(f"See the {lossy['lost']:,} that fell through", "gate_lost", lossy["gate"])])

    # Bottleneck by time.
    slow = max(gate_rows, key=lambda g: g["median_days"] or 0)
    if slow["median_days"]:
        add(85, "bad" if slow["median_days"] >= 3 else "neutral",
            f"Applications wait longest at {slow['label']}",
            f"Getting through it usually takes {_plain_days(slow['median_days'])}, and "
            f"{slow['waiting']:,} are waiting there now.",
            "\"Usually\" is the middle value: of the applications that got through this step, "
            "half took less time and half took more.",
            [_drill("See how long each took", "gate_passed", slow["gate"]),
             _drill(f"See the {slow['waiting']:,} waiting", "gate_waiting", slow["gate"])
             if slow["waiting"] else None])

    # Completion rate.
    rate = outcome["completion_rate"]
    if rate is not None:
        prev = prev_outcome["completion_rate"]
        one_in = round(100 / rate) if rate else None
        if not rate:
            title = "None of these applications has been paid out yet"
        elif rate < 50 and 2 <= one_in <= 20:
            title = f"About 1 in {one_in} applications has been paid out"
        else:
            title = f"{rate:.0f}% of applications have been paid out"
        # The period before has had longer to finish, so a lower rate now is
        # expected and is not on its own bad news; a higher one is.
        before = (f" The period before stands at {prev:.0f}%, having had longer to finish."
                  if prev is not None else "")
        waiting = outcome["in_progress"]
        add(80, "good" if prev is not None and rate > prev + 2 else "neutral", title,
            (f"Another {waiting:,} {'is' if waiting == 1 else 'are'} still on the way through the steps."
             if waiting else "Every other application has been closed."),
            f"Out of the applications submitted in this period, how many have been paid out so far.{before}",
            [_drill(f"See the {outcome['disbursed']:,} paid out", "outcome", "DISBURSED"),
             _drill(f"See the {waiting:,} still in progress", "outcome", "IN_PROGRESS") if waiting else None])

    # Top rejection reason.
    rej = rejections(records, start, end)
    if rej["total"] and rej["categories"]:
        top = rej["categories"][0]
        stage = max(rej["by_stage"], key=lambda s: s["count"])
        add(75, "bad", f"\"{top['label']}\" is the most common reason for turning applications down",
            f"{top['count']:,} of the {rej['total']:,} rejections in this period, mostly at {stage['label']}.",
            "Each reason the team typed when rejecting is sorted into a group by its wording; "
            "this group comes up most often.",
            [_drill(f"See the {top['count']:,} rejected for this", "rejection_category", top["key"])])

    # Corrections.
    corr = corrections(records, start, end, now)
    a, c = corr["outcome_corrected"], corr["outcome_clean"]
    if corr["corrected"] and a["median_days_to_disburse"] and c["median_days_to_disburse"]:
        extra = a["median_days_to_disburse"] - c["median_days_to_disburse"]
        if extra >= 0.4:
            add(70, "bad" if extra > 1 else "neutral",
                f"Sending an application back for corrections delays it by {_plain_days(extra)}",
                f"{corr['corrected_pct']:.0f}% of applications were sent back at least once, and "
                f"{corr['open_now']:,} are waiting on the applicant now.",
                "Compares how long applications took to be paid out when they were sent back for "
                "corrections with when they were not (the middle value of each).",
                [_drill("See the sent-back ones", "time_to_pay", "corrected"),
                 _drill("See the others", "time_to_pay", "clean")])

    # Inflow vs outflow.
    flows = flow(records, start, end)
    closed = flows["total_disbursed"] + flows["total_rejected"]
    why_flow = ("Counts what came in each day against what was closed - paid out or turned down - "
                "over the same days.")
    to_flow = {"label": "Open Applications vs disbursements", "tab": "flow"}
    if flows["net_backlog_change"] > 0 and flows["total_submitted"]:
        add(65, "bad", "Work is piling up: more came in than was finished",
            f"{flows['total_submitted']:,} applications came in; {closed:,} were paid out or turned down.",
            why_flow, [to_flow])
    elif flows["net_backlog_change"] < 0:
        add(55, "good", "The team is clearing the queue: more was finished than came in",
            f"{closed:,} applications were paid out or turned down, against {flows['total_submitted']:,} new ones.",
            why_flow, [to_flow])

    # Busiest day.
    days = flows["days"]
    busy_days = [d["submitted"] for d in days if d["submitted"]]
    busiest = max(days, key=lambda d: d["submitted"]) if busy_days else None
    typical = max(1, round(median(busy_days))) if busy_days else None
    # Worth a card only when that day stood out from the rest.
    if busiest and busiest["submitted"] >= max(typical * 1.5, typical + 2):
        add(50, "neutral", f"{_fmt_day(busiest['date'])} was the busiest day",
            f"{_plural(busiest['submitted'], 'application')} came in, against about {typical:,} on a typical day.",
            "A typical day is the middle value across the days on which anything was submitted.",
            [{"label": "See what happened that day", "day": busiest["date"]}])

    # Product conversion spread.
    products = [p for p in by_product(group) if p["submitted"] >= 10]
    if len(products) > 1:
        best = max(products, key=lambda p: p["completion_rate"] or 0)
        worst = min(products, key=lambda p: p["completion_rate"] or 0)
        if (best["completion_rate"] or 0) - (worst["completion_rate"] or 0) >= 5:
            add(58, "neutral", f"{best['label']} applications are the most likely to be paid out",
                f"{best['completion_rate']:.0f}% of them were paid out, against "
                f"{worst['completion_rate']:.0f}% for {worst['label']}.",
                "For each product, how many of the applications submitted in this period have been "
                "paid out so far. Products with fewer than 10 applications are left out.",
                [{"label": f"Open {worst['label']}", "tab": f"product-{worst['key']}"},
                 {"label": f"Open {best['label']}", "tab": f"product-{best['key']}"}])

    # Ageing open applications (all time, not just the cohort).
    old = _stale(records, now)
    if old:
        gate, n = Counter(r.current_gate for r in old).most_common(1)[0]
        add(68, "bad", f"{_plural(len(old), 'open application')} {'has' if len(old) == 1 else 'have'} "
            f"not moved in over {STALE_DAYS} days",
            f"{n:,} of them are waiting at {GATE_LABELS[gate]}, more than at any other step.",
            "Applications still open, from any date, whose last step was more than "
            f"{STALE_DAYS} days ago. Worth chasing or closing.",
            [_drill(f"See the {_plural(len(old), 'application')}", "stale")])

    found.sort(key=lambda i: -i["score"])
    return [_strip(i) for i in found]


STALE_DAYS = 30


def _stale(records, now):
    """Open applications, from any date, that have not moved for STALE_DAYS."""
    return [r for r in records
            if not r.closed and r.waiting_since() and _days(now - r.waiting_since()) >= STALE_DAYS]


def _plain_days(value):
    """A duration in words a card can carry: "2 days", "half a day"."""
    if value < 0.75:
        return "under a day" if value >= 0.4 else "a few hours"
    days = round(value)
    return "a day" if days == 1 else f"{days} days"


def _strip(insight):
    insight = dict(insight)
    insight.pop("score", None)
    return insight


# --- dashboard blocks (the deck's overview and product pages) ---------------

from .status import STATUS_GROUP_LABELS, STATUS_GROUPS  # noqa: E402

AMOUNT_BANDS = [
    ("Under 250k", 0, 250_000),
    ("250k - 500k", 250_000, 500_000),
    ("500k - 1m", 500_000, 1_000_000),
    ("1m - 5m", 1_000_000, 5_000_000),
    ("5m - 20m", 5_000_000, 20_000_000),
    ("20m - 50m", 20_000_000, 50_000_000),
    ("Over 50m", 50_000_000, None),
]


def band_of(amount):
    for label, low, high in AMOUNT_BANDS:
        if amount >= low and (high is None or amount < high):
            return label
    return AMOUNT_BANDS[0][0]


def month_key(record):
    day = local_date(record.at["SUBMITTED"])
    return f"{day.year:04d}-{day.month:02d}" if day else None


def months_between(start, end):
    keys, year, month = [], start.year, start.month
    while (year, month) <= (end.year, end.month):
        keys.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return keys


def dashboard_block(group, start, end):
    """Everything one deck page shows, for the applications in `group`.

    `group` is already narrowed to the window and the product, so every
    figure on the page - tiles, pies, bars - counts the same applications and
    they reconcile with each other.
    """
    statuses = Counter(r.status for r in group)
    groups = Counter(STATUS_GROUPS.get(r.status, "IN_PROGRESS") for r in group)
    disbursed = [r for r in group if r.disbursed]
    to_disburse = [
        _days(r.at["DISBURSED"] - r.at["SUBMITTED"])
        for r in disbursed if r.at["DISBURSED"] and r.at["SUBMITTED"]
    ]

    # Per month submitted: how many there were, and where they stand today.
    # The last three feed the trend lines behind the page's headline tiles.
    months = {key: {"count": 0, "value": 0.0, "disbursed": 0, "disbursed_value": 0.0, "in_progress": 0, "pending": 0,
                    "rejected": 0, "by_product": Counter()}
              for key in months_between(start, end)}
    standing = {"IN_PROGRESS": "in_progress", "PENDING": "pending", "REJECTED": "rejected"}
    for r in group:
        bucket = months.get(month_key(r))
        if bucket is not None:
            bucket["count"] += 1
            bucket["value"] += r.amount
            bucket["disbursed"] += 1 if r.disbursed else 0
            bucket["disbursed_value"] += r.amount if r.disbursed else 0
            bucket["by_product"][r.product] += 1
            state = standing.get(STATUS_GROUPS.get(r.status, "IN_PROGRESS"))
            if state:
                bucket[state] += 1

    bands = Counter(band_of(r.amount) for r in group)
    states = Counter(r.state for r in group)
    officers = defaultdict(lambda: {"count": 0, "value": 0.0})
    for r in disbursed:
        officers[r.officer]["count"] += 1
        officers[r.officer]["value"] += r.amount

    total = len(group)
    return {
        "tiles": {
            "applications": total,
            "requested_value": sum(r.amount for r in group),
            "disbursed": len(disbursed),
            "disbursed_value": sum(r.amount for r in disbursed),
            "in_progress": groups.get("IN_PROGRESS", 0),
            "pending": groups.get("PENDING", 0),
            "rejected": groups.get("REJECTED", 0),
            "completion_rate": _pct(len(disbursed), total),
            "median_days_to_disburse": _median(to_disburse),
        },
        "outcome": [
            {"key": key, "label": label, "count": groups.get(key, 0), "pct": _pct(groups.get(key, 0), total)}
            for key, label in STATUS_GROUP_LABELS
        ],
        "statuses": [
            {"key": code, "label": status_label(code), "count": statuses.get(code, 0)}
            for code in ["PENDING", "REVIEWED", "CREDIT_APPROVED", "CONTROL_APPROVED",
                         "CORRECTION_REQUESTED", "DISBURSED", "REJECTED"]
        ],
        "monthly": [
            {"key": key, "count": m["count"], "value": m["value"], "disbursed": m["disbursed"],
             "disbursed_value": m["disbursed_value"],
             "in_progress": m["in_progress"], "pending": m["pending"], "rejected": m["rejected"],
             "by_product": dict(m["by_product"])}
            for key, m in months.items()
        ],
        "bands": [{"key": label, "label": label, "count": bands.get(label, 0)} for label, _l, _h in AMOUNT_BANDS],
        "states": [{"key": k, "label": k, "count": n} for k, n in states.most_common(8)],
        "officers": [
            {"key": k, "label": k, "count": v["count"], "value": v["value"]}
            for k, v in sorted(officers.items(), key=lambda kv: -kv[1]["value"])[:8]
        ],
    }


def product_split(group):
    rows = []
    for code, model in PRODUCTS.items():
        subset = [r for r in group if r.product == code]
        disbursed = [r for r in subset if r.disbursed]
        rows.append({
            "key": code, "label": model.PRODUCT_LABEL, "count": len(subset),
            "value": sum(r.amount for r in subset),
            "disbursed": len(disbursed), "disbursed_value": sum(r.amount for r in disbursed),
        })
    return rows


# --- drill-down lists --------------------------------------------------------

def status_detail(record, now):
    """One line saying why an application is where it is."""
    if record.rejected:
        return record.rejection_reason or "No reason recorded"
    if record.disbursed:
        took = (
            _days(record.at["DISBURSED"] - record.at["SUBMITTED"])
            if record.at["DISBURSED"] and record.at["SUBMITTED"] else None
        )
        return f"Disbursed in {took:.1f} days" if took is not None else "Disbursed"
    waited = _days(now - record.waiting_since())
    if record.correction_open:
        return f"With the applicant to correct: {record.correction_message or 'no message'} ({waited:.1f} days)"
    return f"Waiting at {GATE_LABELS[record.current_gate]} for {waited:.1f} days"


def drill(records, kind, key, start, end, now=None):
    """The applications behind a clicked bar, slice or row.

    Returns (title, rows). Every chart that can be clicked resolves through
    here, so a number on a chart always matches the length of the list it
    opens.
    """
    now = now or timezone.now()
    group = cohort(records, start, end)
    title, chosen, extra = "", [], None
    gate_span = None

    if kind == "milestone" and key in MILESTONE_KEYS:
        index = MILESTONE_KEYS.index(key)
        chosen = [r for r in group if r.furthest >= index]
        title = f"Reached {MILESTONE_LABELS[key]}"
    elif kind == "lost_after" and key in MILESTONE_KEYS[1:]:
        index = MILESTONE_KEYS.index(key)
        chosen = [r for r in group if r.furthest == index - 1]
        title = f"Did not reach {MILESTONE_LABELS[key]}"
    elif kind in ("gate_rejected", "gate_waiting", "gate_fresh", "gate_stalled", "gate_passed",
                  "gate_lost") and key in GATES:
        index = GATES.index(key)
        arrived = [r for r in group if r.furthest >= index]
        if kind == "gate_lost":
            # As gates() counts "lost": turned down here, or stuck here for STALLED_DAYS.
            chosen = [r for r in arrived
                      if (r.rejected and r.rejected_gate == key)
                      or (r.current_gate == key and r.waiting_since()
                          and _days(now - r.waiting_since()) >= STALLED_DAYS)]
            title = f"Fell through at {GATE_LABELS[key]}"
        elif kind == "gate_rejected":
            chosen = [r for r in arrived if r.rejected and r.rejected_gate == key]
            title = f"Rejected at {GATE_LABELS[key]}"
        elif kind == "gate_passed":
            chosen = [r for r in arrived if r.furthest >= index + 1]
            title = f"Cleared {GATE_LABELS[key]}"
            gate_span = (MILESTONE_KEYS[index], MILESTONE_KEYS[index + 1])
        else:
            chosen = [r for r in arrived if r.current_gate == key]
            if kind == "gate_stalled":
                chosen = [r for r in chosen if _days(now - r.waiting_since()) >= STALLED_DAYS]
                title = f"Stuck at {GATE_LABELS[key]} for {STALLED_DAYS}+ days"
            elif kind == "gate_fresh":
                chosen = [r for r in chosen if _days(now - r.waiting_since()) < STALLED_DAYS]
                title = f"Waiting at {GATE_LABELS[key]} (under {STALLED_DAYS} days)"
            else:
                title = f"Waiting at {GATE_LABELS[key]}"
        extra = "took" if kind == "gate_passed" else "lost" if kind == "gate_lost" else "wait"
    elif kind.startswith("rejection_"):
        rejected = [
            r for r in records
            if r.rejected and _between(r.rejected_at or r.at["SUBMITTED"], start, end)
        ]
        if kind == "rejection_category":
            chosen = [r for r in rejected if categorise(r.rejection_reason, REJECTION_CATEGORIES) == key]
            title = f"Rejected: {key}"
        elif kind == "rejection_reason":
            chosen = [r for r in rejected if _text_key(r.rejection_reason) == key]
            title = f"Rejected: {chosen[0].rejection_reason or '(no reason)'}" if chosen else "Rejected"
        elif kind == "rejection_stage":
            chosen = [r for r in rejected if r.rejected_gate == key]
            title = f"Rejected at {GATE_LABELS.get(key, key)}"
        elif kind == "rejection_product":
            chosen = [r for r in rejected if r.product_label == key]
            title = f"Rejected: {key}"
        elif kind == "rejection_officer":
            chosen = [r for r in rejected if r.officer == key]
            title = f"Rejected, officer {key}"
        elif kind == "rejection_day":
            chosen = [r for r in rejected if local_date(r.rejected_at) and local_date(r.rejected_at).isoformat() == key]
            title = f"Rejected on {key}"
        extra = "rejection"
    elif kind.startswith("correction_"):
        corrected = [r for r in group if r.correction_count]
        if kind == "correction_category":
            chosen = [r for r in corrected if categorise(r.correction_message, CORRECTION_CATEGORIES) == key]
            title = f"Correction: {key}"
        elif kind == "correction_message":
            chosen = [r for r in corrected if _text_key(r.correction_message) == key]
            title = f"Correction: {chosen[0].correction_message or '(no message)'}" if chosen else "Correction"
        elif kind == "correction_stage":
            chosen = [r for r in corrected if r.correction_gate == key]
            title = f"Sent back from {GATE_LABELS.get(key, key)}"
        elif kind == "correction_rounds":
            n = int(key) if key.isdigit() else 1
            chosen = [r for r in corrected if min(r.correction_count, 3) == n]
            title = f"Corrected {'3+ times' if n == 3 else ('once' if n == 1 else 'twice')}"
        elif kind == "correction_open":
            chosen = [r for r in records if r.correction_open]
            title = "Waiting on the applicant"
        elif kind == "correction_day":
            chosen = [r for r in records if r.correction_at and local_date(r.correction_at).isoformat() == key]
            title = f"Corrections requested on {key}"
        extra = "correction"
    elif kind == "stale":
        chosen = _stale(records, now)
        title = f"Open, no movement for {STALE_DAYS}+ days"
        extra = "wait"
    elif kind == "time_to_pay" and key in ("corrected", "clean"):
        sent_back = key == "corrected"
        chosen = [r for r in group if r.disbursed and bool(r.correction_count) == sent_back]
        title = "Paid out after being sent back for corrections" if sent_back else "Paid out without corrections"
        extra = "paid"
    elif kind == "group" and key in dict(STATUS_GROUP_LABELS):
        chosen = [r for r in group if STATUS_GROUPS.get(r.status, "IN_PROGRESS") == key]
        title = dict(STATUS_GROUP_LABELS)[key]
    elif kind == "status":
        chosen = [r for r in group if r.status == key]
        title = status_label(key)
    elif kind == "all":
        chosen = list(group)
        title = "All requests"
    elif kind == "band":
        chosen = [r for r in group if band_of(r.amount) == key]
        title = f"Loans of {key}"
    elif kind == "state":
        chosen = [r for r in group if r.state == key]
        title = f"Requests from {key}"
    elif kind == "officer":
        chosen = [r for r in group if r.officer == key and r.disbursed]
        title = f"Disbursed by {key}"
    elif kind == "month":
        chosen = [r for r in group if month_key(r) == key]
        title = f"Submitted in {date.fromisoformat(key + '-01').strftime('%B %Y')}" if re.fullmatch(r"\d{4}-\d{2}", key) else key
    elif kind == "product" and key in PRODUCTS:
        chosen = [r for r in group if r.product == key]
        title = f"{PRODUCTS[key].PRODUCT_LABEL} applications"
    elif kind == "outcome":
        mapping = {
            "DISBURSED": lambda r: r.disbursed,
            "REJECTED": lambda r: r.rejected,
            "IN_PROGRESS": lambda r: not r.closed,
            "CORRECTION": lambda r: r.correction_open,
        }
        if key in mapping:
            chosen = [r for r in group if mapping[key](r)]
            title = {"DISBURSED": "Disbursed", "REJECTED": "Rejected", "IN_PROGRESS": "In progress",
                     "CORRECTION": "Waiting on the applicant"}[key]

    rows = []
    for r in sorted(chosen, key=lambda r: r.at["SUBMITTED"] or now, reverse=True):
        row = r.summary()
        if extra == "wait":
            row["detail"] = (f"Waiting {_days(now - r.waiting_since()):.1f} days at {GATE_LABELS[r.current_gate]}"
                             if not r.closed and r.waiting_since() else "")
        elif extra == "took":
            begin, finish = (r.at[k] for k in gate_span)
            row["detail"] = f"Took {_days(finish - begin):.1f} days" if begin and finish and finish >= begin else ""
        elif extra == "lost":
            row["detail"] = (f"Turned down: {r.rejection_reason or '(no reason recorded)'}" if r.rejected
                             else f"Stuck {_days(now - r.waiting_since()):.0f} days")
        elif extra == "paid":
            begin, finish = r.at["SUBMITTED"], r.at["DISBURSED"]
            row["detail"] = f"Paid out {_days(finish - begin):.1f} days after applying" if begin and finish else ""
        elif extra == "rejection":
            row["detail"] = r.rejection_reason or "(no reason recorded)"
        elif extra == "correction":
            row["detail"] = r.correction_message or ""
        else:
            row["detail"] = status_detail(r, now)
        row["reason"] = r.rejection_reason if r.rejected else (r.correction_message if r.correction_open else "")
        rows.append(row)
    return title, rows
