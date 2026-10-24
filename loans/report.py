"""The period report: what the dashboard shows for a span of months, in one
document, set against the span before - with a Cash for Car deep dive and a
checklist that flags what needs a decision.

Run it with `manage.py period_report` (see that command for the options).

Every figure comes from the same functions the dashboard uses - journey.py
for the pipeline, views._commission for Dash vs Floauto - so the report and
the dashboard always agree for the same dates. The only figures computed here
are the Cash for Car ones the dashboard does not show (Floauto evaluation,
offers accepted, loan-to-value, vehicles, scorecard), read straight from
cash_for_car_request.

The report holds totals only - no applicant names - so it can be shared.
"""

from collections import Counter
from datetime import date, timedelta
from statistics import median

from itertools import zip_longest

from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone

from . import journey
from .models import PRODUCTS, CashForCarRequest
from .views import _commission


class Everyone:
    """The report runs from the command line, with no signed-in user: it sees
    what any signed-in user sees."""
    is_authenticated = True


# --- periods ------------------------------------------------------------------

def _add_months(day, n):
    """The first day of the month `n` months from `day`'s month."""
    index = day.year * 12 + day.month - 1 + n
    return date(index // 12, index % 12 + 1, 1)


def _month_end(day):
    return _add_months(day, 1) - timedelta(days=1)


def default_period(months, today):
    """The last `months` complete calendar months before today."""
    end = today.replace(day=1) - timedelta(days=1)
    return _add_months(end, -(months - 1)), end


def previous_period(start, end):
    """The same number of whole months before; for other dates, the same number of days."""
    if start.day == 1 and end == _month_end(end):
        months = (end.year - start.year) * 12 + end.month - start.month + 1
        return _add_months(start, -months), start - timedelta(days=1)
    length = (end - start).days + 1
    return start - timedelta(days=length), start - timedelta(days=1)


def month_spans(start, end):
    """(label, first day, last day) for each calendar month in [start, end], clipped to it."""
    spans, first = [], start.replace(day=1)
    while first <= end:
        spans.append((first.strftime("%b %Y"), max(first, start), min(_month_end(first), end)))
        first = _add_months(first, 1)
    return spans


def period_label(start, end):
    if start.year == end.year:
        return f"{start.day} {start:%b} – {end.day} {end:%b %Y}"
    return f"{start.day} {start:%b %Y} – {end.day} {end:%b %Y}"


def change(a, b, points=False):
    """A against B: percentage points for rates, otherwise % change. None when it can't be said."""
    if a is None or b is None:
        return None
    if points:
        return round(a - b, 1)
    return round((a - b) * 100 / b, 1) if b else None


# --- the checklist ---------------------------------------------------------------
# Starting points, not rules: adjust them to what the business considers
# healthy. Each returns "green", "amber" or "red" (or None: not enough data).

def _band(value, green, amber, higher_is_better=True):
    if value is None:
        return None
    if higher_is_better:
        return "green" if value >= green else "amber" if value >= amber else "red"
    return "green" if value <= green else "amber" if value <= amber else "red"


# --- Cash for Car ---------------------------------------------------------------

CFC_FIELDS = [
    "loan_amount", "tenor_months", "status", "floauto_evaluated", "applicant_accepted", "applicant_declined",
    "floauto_ltv", "security_coverage_ratio", "floauto_recommended_amount", "flograde_valuation",
    "floauto_valuation", "vehicle_make", "vehicle_year", "scorecard_verdict", "buyout_obligation",
    "floauto_sale_guarantee", "fee_dash_share", "fee_floauto_share", "rate_dash_share", "rate_floauto_share",
]


def _ratio(value):
    """Loan-to-value as a fraction. Stored as 0.45 - or, if a row says 45, as a percentage."""
    if value is None:
        return None
    value = float(value)
    return value / 100 if value > 1.5 else value


def _num(value, fmt):
    """A figure for the checklist, or a dash when there is none."""
    return "–" if value is None else fmt.format(value)


def _mean(values):
    values = [float(v) for v in values if v is not None]
    return round(sum(values) / len(values), 2) if values else None


def _pct(part, whole):
    return round(part * 100 / whole, 1) if whole else None


def cash_for_car(user, start, end):
    """Cash for Car beyond the dashboard, for the applications submitted in [start, end]."""
    rows = list(CashForCarRequest.objects.visible_to(user)
                .filter(created_at__date__range=(start, end)).values(*CFC_FIELDS))
    paid = [r for r in rows if r["status"] == "DISBURSED"]
    amounts = [float(r["loan_amount"] or 0) for r in paid]
    ltv = [_ratio(r["floauto_ltv"]) for r in paid if r["floauto_ltv"] is not None]
    with_recommendation = [r for r in paid if r["floauto_recommended_amount"] is not None]
    above = [r for r in with_recommendation if float(r["loan_amount"] or 0) > float(r["floauto_recommended_amount"])]
    years = [r["vehicle_year"] for r in rows if r["vehicle_year"]]
    n = len(rows)
    return {
        "applications": n,
        "disbursed": len(paid),
        "amount_disbursed": sum(amounts),
        "share_paid_out": _pct(len(paid), n),
        "average_loan": round(sum(amounts) / len(amounts)) if amounts else None,
        "median_tenor": median([r["tenor_months"] for r in paid if r["tenor_months"]]) if any(r["tenor_months"] for r in paid) else None,
        # The Cash for Car steps beyond the shared approval spine.
        "steps": [
            ("Applications", n, 100.0 if n else None),
            ("Evaluated by Floauto", sum(1 for r in rows if r["floauto_evaluated"]), _pct(sum(1 for r in rows if r["floauto_evaluated"]), n)),
            ("Offer accepted by the applicant", sum(1 for r in rows if r["applicant_accepted"]), _pct(sum(1 for r in rows if r["applicant_accepted"]), n)),
            ("Offer declined by the applicant", sum(1 for r in rows if r["applicant_declined"]), _pct(sum(1 for r in rows if r["applicant_declined"]), n)),
            ("Disbursed", len(paid), _pct(len(paid), n)),
        ],
        # Risk on what was paid out.
        "average_ltv": round(_mean(ltv) * 100, 1) if ltv else None,
        "ltv_over_70": _pct(sum(1 for v in ltv if v > 0.70), len(ltv)),
        "average_coverage": _mean(r["security_coverage_ratio"] for r in paid),
        "above_recommended": len(above),
        "above_recommended_pct": _pct(len(above), len(with_recommendation)),
        "buyout_pct": _pct(sum(1 for r in paid if r["buyout_obligation"]), len(paid)),
        "sale_guarantee_pct": _pct(sum(1 for r in paid if r["floauto_sale_guarantee"]), len(paid)),
        "scorecard": Counter((r["scorecard_verdict"] or "Not scored") for r in rows).most_common(),
        "makes": Counter((r["vehicle_make"] or "Not recorded") for r in rows).most_common(5),
        "median_vehicle_age": round(median([end.year - y for y in years]), 1) if years else None,
        # The two splits: fees at disbursement (what the earnings use) and the interest rate.
        "fee_dash": _mean(r["fee_dash_share"] for r in paid),
        "fee_floauto": _mean(r["fee_floauto_share"] for r in paid),
        "rate_dash": _mean(r["rate_dash_share"] for r in paid),
        "rate_floauto": _mean(r["rate_floauto_share"] for r in paid),
        # Gaps that quietly change the figures.
        "missing_fee_shares": sum(1 for r in paid if r["fee_dash_share"] is None or r["fee_floauto_share"] is None),
        "missing_valuation": sum(1 for r in rows if r["floauto_valuation"] is None and r["flograde_valuation"] is None),
        "missing_vehicle_year": sum(1 for r in rows if not r["vehicle_year"]),
    }


# --- the report -----------------------------------------------------------------

def build(start, end, user=None, now=None):
    """Everything the report shows for [start, end], against the period before."""
    user = user or Everyone()
    now = now or timezone.now()
    records = journey.load_records(user)
    prev_start, prev_end = previous_period(start, end)
    group = journey.cohort(records, start, end)
    prev_group = journey.cohort(records, prev_start, prev_end)

    headline = journey.compare(records, (start, end), (prev_start, prev_end))["metrics"]
    by_key = {m["key"]: m for m in headline}

    months = []
    for label, m_start, m_end in month_spans(start, end):
        tiles = journey.dashboard_block(journey.cohort(records, m_start, m_end), m_start, m_end)["tiles"]
        months.append({"label": label, **tiles})

    products = []
    for code, model in PRODUCTS.items():
        now_ = journey.outcome_summary([r for r in group if r.product == code])
        before = journey.outcome_summary([r for r in prev_group if r.product == code])
        products.append({
            "label": model.PRODUCT_LABEL, "now": now_, "before": before,
            "applications_change": change(now_["submitted"], before["submitted"]),
            "amount_change": change(now_["disbursed_value"], before["disbursed_value"]),
            "rate_change": change(now_["completion_rate"], before["completion_rate"], points=True),
        })

    gates = journey.gates(group, now)
    stale = journey._stale(records, now)
    open_now = [r for r in records if not r.closed]
    flows = journey.flow(records, start, end)
    rejections = journey.rejections(records, start, end)
    corrections = journey.corrections(records, start, end, now)
    outcome, prev_outcome = journey.outcome_summary(group), journey.outcome_summary(prev_group)

    cfc = cash_for_car(user, start, end)
    cfc_before = cash_for_car(user, prev_start, prev_end)
    earnings = _commission(user, start, end)
    earnings_before = _commission(user, prev_start, prev_end)
    earnings_by_month = []
    for label, s, e in month_spans(start, end):
        row = _commission(user, s, e)
        earnings_by_month.append({"label": label, "both": row["dash"] + row["floauto"], **row})

    def m(key, side="a"):
        return by_key[key][side]

    rejection_rate = _pct(outcome["rejected"], outcome["submitted"])
    prev_rejection_rate = _pct(prev_outcome["rejected"], prev_outcome["submitted"])
    earned = earnings["dash"] + earnings["floauto"]
    earned_before = earnings_before["dash"] + earnings_before["floauto"]
    stale_share = _pct(len(stale), len(open_now))
    days_change = change(m("median_days_to_disburse"), m("median_days_to_disburse", "b"), points=True)
    backlog_share = _pct(flows["net_backlog_change"], flows["total_submitted"]) if flows["total_submitted"] else None

    checklist = [
        ("Demand", "Are we getting more or fewer applications?",
         f"{m('submitted'):,} vs {m('submitted', 'b'):,}", by_key["submitted"]["change"],
         _band(by_key["submitted"]["change"], -5, -20), "Down more than 20%: check marketing, channels and the officers' pipelines."),
        ("Conversion", "Is a bigger or smaller share being paid out?",
         f"{_num(m('completion_rate'), '{:.1f}%')} vs {_num(m('completion_rate', 'b'), '{:.1f}%')}", by_key["completion_rate"]["change"],
         _band(by_key["completion_rate"]["change"], -2, -5),
         "Down more than 5 points: look at where applications fall through (Pipeline) and why (Rejections). The earlier period has had longer to finish."),
        ("Speed", "Are loans paid out faster or slower?",
         f"{_num(m('median_days_to_disburse'), '{:.1f}')} vs {_num(m('median_days_to_disburse', 'b'), '{:.1f}')} days", days_change,
         _band(days_change, 1, 3, higher_is_better=False), "Slower by more than 3 days: find the slowest step in Pipeline health and its queue."),
        ("Backlog", "Is work piling up?",
         f"{flows['net_backlog_change']:+,} applications", backlog_share,
         _band(backlog_share, 0, 10, higher_is_better=False), "Piling up: add review capacity or clear old applications."),
        ("Stuck work", "How much open work has not moved in 30 days?",
         f"{len(stale):,} of {len(open_now):,} open", stale_share,
         _band(stale_share, 10, 25, higher_is_better=False), "Over a quarter idle: chase or close them; see the gate holding most."),
        ("Rejections", "Are we turning down more?",
         f"{_num(rejection_rate, '{:.1f}%')} vs {_num(prev_rejection_rate, '{:.1f}%')}", change(rejection_rate, prev_rejection_rate, points=True),
         _band(change(rejection_rate, prev_rejection_rate, points=True), 2, 5, higher_is_better=False),
         "Up more than 5 points: read the top reasons - is it applicant quality, or a policy change?"),
        ("Corrections", "How often do applications go back to the applicant?",
         f"{_num(corrections['corrected_pct'], '{:.1f}%')} sent back", corrections["corrected_pct"],
         _band(corrections["corrected_pct"], 20, 40, higher_is_better=False), "Above 40%: tighten the application form and the checklist given to applicants."),
        ("Cash for Car earnings", "Did Dash and Floauto earn more or less?",
         f"₦{earned:,.0f} vs ₦{earned_before:,.0f}", change(earned, earned_before),
         _band(change(earned, earned_before), 0, -20), "Down more than 20%: fewer or smaller Cash for Car payouts - check volume and offer acceptance."),
        ("Cash for Car risk", "Are we lending above Floauto's recommended amount?",
         f"{cfc['above_recommended']:,} of {cfc['disbursed']:,} paid out", cfc["above_recommended_pct"],
         _band(cfc["above_recommended_pct"], 0, 10, higher_is_better=False), "Any at all: confirm each was an approved exception."),
        ("Cash for Car risk", "How much of each car's value are we lending (loan-to-value)?",
         f"{_num(cfc['average_ltv'], '{:.1f}%')} on average", cfc["average_ltv"],
         _band(cfc["average_ltv"], 60, 75, higher_is_better=False), "Above 75%: less cushion if a car must be sold; review the LTV limit."),
        ("Data quality", "Are Cash for Car fee shares recorded on every disbursed loan?",
         f"{cfc['missing_fee_shares']:,} missing", cfc["missing_fee_shares"],
         _band(cfc["missing_fee_shares"], 0, 0, higher_is_better=False), "Missing shares count as 0% earned: fix them in the loan system."),
    ]

    return {
        "start": start, "end": end, "label": period_label(start, end),
        "prev_start": prev_start, "prev_end": prev_end, "prev_label": period_label(prev_start, prev_end),
        "generated": timezone.localtime(now),
        "headline": headline,
        "insights": journey.insights(records, start, end, now)[:6],
        "months": months,
        "products": products,
        "gates": gates,
        "stale": len(stale),
        "stale_by_gate": [(journey.GATE_LABELS[g], n) for g, n in Counter(r.current_gate for r in stale).most_common()],
        "open_now": len(open_now),
        "flow": flows,
        "rejections": rejections,
        "corrections": corrections,
        "cfc": cfc, "cfc_before": cfc_before,
        "cfc_changes": {
            "applications": change(cfc["applications"], cfc_before["applications"]),
            "disbursed": change(cfc["disbursed"], cfc_before["disbursed"]),
            "amount": change(cfc["amount_disbursed"], cfc_before["amount_disbursed"]),
            "earned": change(earned, earned_before),
        },
        "earnings": earnings, "earnings_before": earnings_before, "earned": earned, "earned_before": earned_before,
        "earnings_by_month": earnings_by_month,
        "checklist": [
            {"area": a, "question": q, "value": v, "flag": f, "action": act} for a, q, v, _score, f, act in checklist
        ],
    }


def render(start, end, user=None, now=None):
    """The report as one self-contained HTML page."""
    report = build(start, end, user, now)
    pairs = [(a or ("", ""))[0:2] + (b or ("", ""))[0:2]
             for a, b in zip_longest(report["cfc"]["scorecard"], report["cfc"]["makes"])]
    return render_to_string("loans/report.html", {
        "r": report,
        "cfc_pairs": pairs,
        "blank_rows": range(8),
        "database": settings.DATABASES["loans"]["NAME"],
        "sample": settings.SAMPLE_DB,
    })
