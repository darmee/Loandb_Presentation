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

    def test_a_day_carries_the_hours_it_was_active(self):
        # 08:30 UTC is 09:30 in Lagos. Submitted on the 1st, reviewed and
        # credit-approved on the following days, at the same hour.
        early = rec(1, created=datetime(2026, 9, 1, 8, 30, tzinfo=timezone.utc), stages=2)
        late = rec(2, created=datetime(2026, 9, 1, 15, 5, tzinfo=timezone.utc))
        days = {d["date"]: d for d in journey.daily_activity([early, late], *SEPT)}
        self.assertEqual((days["2026-09-01"]["first_time"], days["2026-09-01"]["last_time"]), ("09:30", "16:05"))
        self.assertEqual((days["2026-09-02"]["first_time"], days["2026-09-02"]["last_time"]), ("09:30", "09:30"))
        # Credit approval is not one of the report's five steps: it is
        # counted on its day, but it does not set the day's hours.
        self.assertEqual(days["2026-09-03"]["credit_approved"], 1)
        self.assertIsNone(days["2026-09-03"]["first_time"])
        self.assertIsNone(days["2026-09-04"]["first_time"])

    def test_an_insight_card_carries_few_figures(self):
        # Read from across a room: a card with five numbers in it is a table.
        import re
        records = [rec(i, created=datetime(2026, 8 + i % 2, 1 + i % 20, 9, 0, tzinfo=timezone.utc), stages=i % 5,
                       rejected=i % 4 == 0, reason="Adverse credit bureau record.", corrections=i % 2,
                       product=["cashback", "cash-for-car", "public-sector"][i % 3])
                   for i in range(1, 121)]
        found = journey.insights(records, *SEPT, now=NOW)
        self.assertTrue(found)
        for insight in found:
            with self.subTest(title=insight["title"]):
                figures = re.findall(r"\d[\d,.]*", f"{insight['title']} {insight['detail']}")
                self.assertLessEqual(len(figures), 3, insight)

    def test_a_loan_counts_in_the_period_it_was_applied_for(self):
        # Applied for on 29 August, paid out on 2 September (four steps, a day
        # each): it is August's loan, as the Monthly overview shows it.
        late = rec(1, created=datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc), stages=4, amount=500_000)
        aug = (date(2026, 8, 1), date(2026, 8, 31))
        result = journey.compare([late], SEPT, aug)
        metrics = {m["key"]: (m["a"], m["b"]) for m in result["metrics"]}
        self.assertEqual(metrics["disbursed"], (0, 1))
        self.assertEqual(metrics["disbursed_value"], (0, 500_000))
        # The chart puts it on the day it was applied for.
        days = {d["date"]: d for d in result["b"]["days"]}
        self.assertEqual((days["2026-08-29"]["disbursed"], days["2026-08-29"]["disbursed_value"]), (1, 500_000))

    def test_a_period_shows_what_the_overview_shows_for_the_same_dates(self):
        records = [rec(i, created=datetime(2026, 9, 1 + i % 28, 9, 0, tzinfo=timezone.utc), stages=i % 5,
                       rejected=i % 6 == 0, amount=100_000 * i) for i in range(1, 60)]
        result = journey.compare(records, SEPT, (date(2026, 8, 1), date(2026, 8, 31)))
        shown = {m["key"]: m["a"] for m in result["metrics"]}
        tiles = journey.dashboard_block(journey.cohort(records, *SEPT), *SEPT)["tiles"]
        self.assertEqual(shown, {"submitted": tiles["applications"], "disbursed": tiles["disbursed"],
                                 "disbursed_value": tiles["disbursed_value"], "rejected": tiles["rejected"],
                                 "completion_rate": tiles["completion_rate"],
                                 "median_days_to_disburse": tiles["median_days_to_disburse"]})
        # And the chart's days add up to the table.
        for key in ("submitted", "disbursed", "disbursed_value", "rejected"):
            self.assertEqual(sum(d[key] for d in result["a"]["days"]), shown[key])

    def test_each_month_carries_the_amount_disbursed_on_its_applications(self):
        records = [rec(1, stages=4, amount=300_000), rec(2, stages=4, amount=200_000), rec(3, stages=2, amount=900_000)]
        block = journey.dashboard_block(records, *SEPT)
        self.assertEqual(block["tiles"]["disbursed_value"], 500_000)
        self.assertEqual([m["disbursed_value"] for m in block["monthly"]], [500_000])

    def busy_records(self):
        return [rec(i, created=datetime(2026, 8 + i % 2, 1 + i % 20, 9, 0, tzinfo=timezone.utc), stages=i % 5,
                    rejected=i % 4 == 0, reason="Adverse credit bureau record.", corrections=i % 2,
                    correction_open=i % 9 == 0, product=["cashback", "cash-for-car", "public-sector"][i % 3])
                for i in range(1, 121)]

    def test_every_insight_says_how_it_was_worked_out_and_where_to_check(self):
        tabs = {"overview", "daily", "monthly", "flow", "compare"} | {f"product-{c}" for c in journey.PRODUCTS}
        found = journey.insights(self.busy_records(), *SEPT, now=NOW)
        self.assertGreaterEqual(len(found), 5)
        for insight in found:
            with self.subTest(title=insight["title"]):
                self.assertIn(insight["tone"], {"good", "bad", "neutral"})
                self.assertTrue(insight["why"])
                self.assertTrue(1 <= len(insight["actions"]) <= 2)
                for action in insight["actions"]:
                    self.assertTrue(action["label"])
                    if "tab" in action:
                        self.assertIn(action["tab"], tabs)
                    else:
                        self.assertTrue({"drill", "day", "compare"} & action.keys())

    def test_a_button_opens_as_many_applications_as_it_says(self):
        import re
        records = self.busy_records()
        checked = 0
        for insight in journey.insights(records, *SEPT, now=NOW):
            for action in insight["actions"]:
                figure = re.search(r"\d[\d,]*", action["label"])
                if "drill" not in action or not figure:
                    continue
                with self.subTest(button=action["label"]):
                    title, rows = journey.drill(records, action["drill"]["kind"], action["drill"]["key"], *SEPT, now=NOW)
                    self.assertTrue(title)
                    self.assertEqual(len(rows), int(figure.group(0).replace(",", "")))
                    checked += 1
        self.assertGreaterEqual(checked, 4)

    def test_the_comparison_button_carries_the_two_periods_the_card_compared(self):
        found = journey.insights(self.busy_records(), *SEPT, now=NOW)
        compare = [a["compare"] for i in found for a in i["actions"] if "compare" in a]
        self.assertEqual(compare, [{"a": ["2026-09-01", "2026-09-30"], "b": ["2026-08-02", "2026-08-31"]}])
