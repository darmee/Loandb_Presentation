"""The period report: its periods, that its figures match the dashboard's, and the command."""

import tempfile
from datetime import date, datetime, timezone
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.test import SimpleTestCase

from loans import journey, report
from loans.models import CashbackRequest, CashForCarRequest
from loans.views import _commission

from .base import LoanDataTestCase

Q3 = (date(2026, 7, 1), date(2026, 9, 30))
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)


def at(y, m, d):
    return datetime(y, m, d, 10, 0, tzinfo=timezone.utc)


class PeriodTests(SimpleTestCase):
    def test_the_default_is_the_last_three_complete_months(self):
        self.assertEqual(report.default_period(3, date(2026, 10, 23)), Q3)
        self.assertEqual(report.default_period(1, date(2026, 1, 5)), (date(2025, 12, 1), date(2025, 12, 31)))

    def test_the_period_before_is_the_same_months_before(self):
        self.assertEqual(report.previous_period(*Q3), (date(2026, 4, 1), date(2026, 6, 30)))
        # Not whole months: the same number of days just before.
        self.assertEqual(report.previous_period(date(2026, 8, 11), date(2026, 8, 20)), (date(2026, 8, 1), date(2026, 8, 10)))

    def test_months_split_the_period(self):
        self.assertEqual([label for label, *_ in report.month_spans(*Q3)], ["Jul 2026", "Aug 2026", "Sep 2026"])


class ReportTests(LoanDataTestCase):
    def setUp(self):
        super().setUp()
        steps = dict(reviewed=True, reviewed_at=at(2026, 8, 3), credit_approved=True, credit_approved_at=at(2026, 8, 4),
                     control_approved=True, control_approved_at=at(2026, 8, 5), disbursed=True)
        # In Q3: two Cashback (one paid out), and a Cash for Car loan paid out in September.
        self.make_loan(CashbackRequest, pk=1, created_at=at(2026, 8, 1), loan_amount=300_000,
                       disbursed_at=at(2026, 8, 6), **steps)
        self.make_loan(CashbackRequest, pk=2, created_at=at(2026, 9, 10), loan_amount=200_000)
        self.make_loan(CashForCarRequest, pk=3, created_at=at(2026, 8, 20), loan_amount=2_000_000,
                       disbursed_at=at(2026, 9, 2), fee_dash_share="1.5", fee_floauto_share="1.0",
                       floauto_evaluated=True, applicant_accepted=True, floauto_ltv="0.80",
                       floauto_recommended_amount=1_500_000, vehicle_make="Toyota", vehicle_year=2018,
                       scorecard_verdict="PASS", **steps)
        # In Q2, the period before: one Cash for Car application, not paid out.
        self.make_loan(CashForCarRequest, pk=4, created_at=at(2026, 5, 5), loan_amount=900_000)

    def test_the_figures_are_the_dashboards(self):
        built = report.build(*Q3, now=NOW)
        records = journey.load_records(report.Everyone())
        tiles = journey.dashboard_block(journey.cohort(records, *Q3), *Q3)["tiles"]
        headline = {m["key"]: m for m in built["headline"]}
        self.assertEqual(headline["submitted"]["a"], tiles["applications"])
        self.assertEqual(headline["disbursed"]["a"], tiles["disbursed"])
        self.assertEqual(headline["disbursed_value"]["a"], tiles["disbursed_value"])
        self.assertEqual((headline["submitted"]["a"], headline["submitted"]["b"]), (3, 1))
        # The months add up to the period.
        self.assertEqual(sum(m["applications"] for m in built["months"]), 3)
        self.assertEqual(sum(m["disbursed_value"] for m in built["months"]), 2_300_000)

    def test_cash_for_car_in_depth(self):
        built = report.build(*Q3, now=NOW)
        cfc = built["cfc"]
        self.assertEqual((cfc["applications"], cfc["disbursed"], cfc["amount_disbursed"]), (1, 1, 2_000_000))
        self.assertEqual(dict((label, n) for label, n, _share in cfc["steps"])["Evaluated by Floauto"], 1)
        self.assertEqual((cfc["average_ltv"], cfc["ltv_over_70"]), (80.0, 100.0))
        self.assertEqual(cfc["above_recommended"], 1)             # 2m lent against 1.5m recommended
        self.assertEqual(cfc["makes"], [("Toyota", 1)])
        # Earnings are the dashboard card's, month by month adding up to the total.
        self.assertEqual(built["earnings"], _commission(report.Everyone(), *Q3))
        self.assertEqual((built["earnings"]["dash"], built["earnings"]["floauto"]), (30_000.0, 20_000.0))
        self.assertEqual(sum(m["both"] for m in built["earnings_by_month"]), 50_000.0)
        self.assertEqual(built["cfc_before"]["applications"], 1)

    def test_the_checklist_flags_lending_above_the_recommendation(self):
        flags = {c["question"]: c["flag"] for c in report.build(*Q3, now=NOW)["checklist"]}
        self.assertEqual(flags["Are we lending above Floauto's recommended amount?"], "red")
        self.assertEqual(flags["How much of each car's value are we lending (loan-to-value)?"], "red")
        self.assertEqual(flags["Are Cash for Car fee shares recorded on every disbursed loan?"], "green")

    def test_the_command_writes_a_report_with_no_applicant_names(self):
        with tempfile.TemporaryDirectory() as folder:
            out = StringIO()
            call_command("period_report", "--start", "2026-07-01", "--end", "2026-09-30", "--out", folder, stdout=out)
            page = Path(folder, "loan-report-2026-07-01-to-2026-09-30.html").read_text(encoding="utf-8")
        self.assertIn("Loan pipeline report: 1 Jul – 30 Sep 2026", page)
        self.assertIn("Cash for Car in depth", page)
        self.assertIn("Decision checklist", page)
        self.assertNotIn("Test Applicant", page)    # the test loans' name
        self.assertNotIn("REF-0000", page)
