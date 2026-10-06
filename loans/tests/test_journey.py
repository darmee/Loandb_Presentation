"""The journey analyses, on hand-built records - no database needed."""

from datetime import date, datetime, timedelta, timezone

from django.test import SimpleTestCase, override_settings

from loans import journey

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def rec(pk=1, product="cashback", created=None, stages=0, rejected=False, reason="",
        correction_open=False, corrections=0, correction_message="", amount=100_000):
    """A Record that reached `stages` milestones after submission, one a day."""
    created = created or datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    row = {
        "id": pk, "reference_no": f"REF-{pk}", "first_name": "Ada", "last_name": "Obi",
        "loan_amount": amount, "account_officer": "a. salami", "state": "lagos",
        "created_at": created,
        "rejected": rejected, "rejected_at": created + (stages + 1) * DAY if rejected else None,
        "rejection_reason": reason, "rejected_stage": None,
        "correction_requested": correction_open,
        "correction_requested_at": created + DAY if (corrections or correction_open) else None,
        "correction_count": corrections, "correction_stage": "REVIEW" if corrections else None,
        "correction_message": correction_message,
    }
    for index, (_key, _label, flag, column) in enumerate(journey.MILESTONES[1:], start=1):
        row[flag] = index <= stages
        row[column] = created + index * DAY if index <= stages else None
    if rejected:
        row["status"] = "REJECTED"
    elif stages == 4:
        row["status"] = "DISBURSED"
    elif correction_open:
        row["status"] = "CORRECTION_REQUESTED"
    else:
        row["status"] = ["PENDING", "REVIEWED", "CREDIT_APPROVED", "CONTROL_APPROVED"][stages]
    return journey.Record(product, product.title(), row)


SEPT = (date(2026, 9, 1), date(2026, 9, 30))


@override_settings(TIME_ZONE="Africa/Lagos")
class FunnelTests(SimpleTestCase):
    def setUp(self):
        self.records = [
            rec(1, stages=4),
            rec(2, stages=4),
            rec(3, stages=1, rejected=True, reason="Affordability below threshold."),
            rec(4, stages=2),
            rec(5, stages=0),
        ]

    def test_reached_counts_never_increase(self):
        counts = [s["count"] for s in journey.funnel(self.records)]
        self.assertEqual(counts, [5, 4, 3, 2, 2])

    def test_conversion_from_previous_step(self):
        stages = journey.funnel(self.records)
        self.assertEqual(stages[1]["pct_of_previous"], 80.0)
        self.assertEqual(stages[2]["pct_of_previous"], 75.0)
        self.assertEqual(stages[2]["lost_from_previous"], 1)

    def test_every_lost_application_is_rejected_or_waiting_at_that_gate(self):
        for gate in journey.gates(self.records, NOW):
            with self.subTest(gate=gate["gate"]):
                self.assertEqual(gate["arrived"], gate["passed"] + gate["rejected"] + gate["waiting"])

    def test_rejection_is_placed_at_the_gate_it_could_not_pass(self):
        gates = {g["gate"]: g for g in journey.gates(self.records, NOW)}
        self.assertEqual(gates["CREDIT"]["rejected"], 1)
        self.assertEqual(gates["REVIEW"]["rejected"], 0)

    def test_outcome(self):
        o = journey.outcome_summary(self.records)
        self.assertEqual((o["disbursed"], o["rejected"], o["in_progress"]), (2, 1, 2))
        self.assertEqual(o["completion_rate"], 40.0)
        self.assertEqual(o["median_days_to_disburse"], 4.0)


@override_settings(TIME_ZONE="Africa/Lagos")
class DailyTests(SimpleTestCase):
    def test_events_land_on_the_lagos_day_they_happened(self):
        # 23:30 UTC on 1 Sep is 00:30 on 2 Sep in Lagos.
        late = rec(1, created=datetime(2026, 9, 1, 23, 30, tzinfo=timezone.utc))
        days = {d["date"]: d for d in journey.daily_activity([late], *SEPT)}
        self.assertEqual(days["2026-09-01"]["submitted"], 0)
        self.assertEqual(days["2026-09-02"]["submitted"], 1)

    def test_day_detail_lists_each_event(self):
        r = rec(1, stages=2)
        detail = journey.day_detail([r], date(2026, 9, 2))
        self.assertEqual(detail["totals"]["reviewed"], 1)
        self.assertEqual([row["event"] for row in detail["rows"]], ["reviewed"])

    def test_every_day_in_the_window_is_present(self):
        self.assertEqual(len(journey.daily_activity([], *SEPT)), 30)


@override_settings(TIME_ZONE="Africa/Lagos")
class TextTests(SimpleTestCase):
    def test_categorise_rejection_reasons(self):
        cases = {
            "Affordability below threshold.": "Affordability",
            "Adverse credit bureau record": "Credit history",
            "BVN mismatch": "Identity / KYC",
            "Applicant withdrew the request.": "Applicant withdrew",
            "": "Not recorded",
            "Something unusual": "Other",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(journey.categorise(text, journey.REJECTION_CATEGORIES), expected)

    def test_reasons_differing_only_in_case_and_punctuation_group_together(self):
        records = [
            rec(1, stages=1, rejected=True, reason="Affordability below threshold."),
            rec(2, stages=1, rejected=True, reason="affordability  below threshold"),
        ]
        result = journey.rejections(records, *SEPT)
        self.assertEqual(result["reasons"][0]["count"], 2)

    def test_normalise_gate(self):
        self.assertEqual(journey.normalise_gate("credit_supervisor"), "CREDIT")
        self.assertEqual(journey.normalise_gate("INTERNAL_CONTROL"), "CONTROL")
        self.assertEqual(journey.normalise_gate("operations"), "DISBURSEMENT")
        self.assertEqual(journey.normalise_gate(None, "REVIEW"), "REVIEW")


@override_settings(TIME_ZONE="Africa/Lagos")
class CorrectionTests(SimpleTestCase):
    def test_corrected_vs_clean_outcomes(self):
        records = [
            rec(1, stages=4, corrections=1, correction_message="Bank statement is illegible"),
            rec(2, stages=1, correction_open=True, corrections=1, correction_message="Upload a valid ID"),
            rec(3, stages=4),
        ]
        c = journey.corrections(records, *SEPT, now=NOW)
        self.assertEqual(c["corrected"], 2)
        self.assertEqual(c["open_now"], 1)
        self.assertEqual(c["outcome_corrected"]["disbursed_pct"], 50.0)
        self.assertEqual(c["outcome_clean"]["disbursed_pct"], 100.0)
        labels = {x["label"] for x in c["categories"]}
        self.assertEqual(labels, {"Bank statement", "Identity document"})


@override_settings(TIME_ZONE="Africa/Lagos")
class DrillAndCompareTests(SimpleTestCase):
    def test_drill_counts_match_the_funnel(self):
        records = [rec(i, stages=i % 5) for i in range(1, 21)]
        for stage in journey.funnel(records):
            _title, rows = journey.drill(records, "milestone", stage["key"], *SEPT, now=NOW)
            self.assertEqual(len(rows), stage["count"])

    def test_compare_reports_change_and_direction(self):
        sept = [rec(i, stages=4) for i in range(1, 5)]
        aug = [rec(10 + i, created=datetime(2026, 8, 5, tzinfo=timezone.utc), stages=4) for i in range(2)]
        result = journey.compare(sept + aug, SEPT, (date(2026, 8, 1), date(2026, 8, 31)))
        submitted = [m for m in result["metrics"] if m["key"] == "submitted"][0]
        self.assertEqual((submitted["a"], submitted["b"], submitted["change"]), (4, 2, 100.0))
        self.assertEqual(submitted["direction"], "good")

    def test_insights_point_at_a_real_tab(self):
        records = [rec(i, stages=i % 5, rejected=(i % 7 == 0), reason="Adverse credit bureau record")
                   for i in range(1, 40)]
        tabs = {"overview", "daily", "funnel", "flow", "rejections", "corrections", "compare"}
        for insight in journey.insights(records, *SEPT, now=NOW):
            self.assertIn(insight["tab"], tabs)
            self.assertIn(insight["tone"], {"good", "bad", "neutral"})
