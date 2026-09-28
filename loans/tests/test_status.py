"""The "click a status to see the requests behind it" drill-down.

The Analytics panel's KPI tiles, its "Final outcome" pie (both the slices and
the legend rows) and the "Request type comparison" pie all carry a link to
the same set of requests, filtered - `?status_group=...` on the product list,
or on "All requests" for the cross-product overview. This is the click path
from "26 Rejected" on the deck through to the reason a specific application
was turned down.
"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from loans.models import CashbackRequest, CashForCarRequest

from .base import LoanDataTestCase


class StatusDrilldownTestCase(LoanDataTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user("clerk", password="not-used")
        self.client.force_login(self.user)


class ProductPanelDrilldownTests(StatusDrilldownTestCase):
    def test_rejected_tile_and_legend_link_to_the_filtered_list(self):
        self.make_loan(
            model=CashbackRequest, pk=1, rejected=True,
            rejection_reason="Payslips did not match the declared employer.",
        )
        self.make_loan(model=CashbackRequest, pk=2, disbursed=True)

        response = self.client.get(reverse("analytics-panel"), {"product": "cashback"})
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()

        expected_url = reverse("product-list", args=["cashback"]) + "?status_group=REJECTED"
        # The KPI tile - coloured to match its "Final outcome" slice...
        self.assertIn(f'<a class="tile tile--coloured" href="{expected_url}"', html)
        # ...and the "Final outcome" legend row both point at it.
        self.assertIn(f'<a class="row" href="{expected_url}"', html)

    def test_the_link_shows_exactly_the_rejected_requests_for_that_product(self):
        self.make_loan(
            model=CashbackRequest, pk=1, rejected=True,
            rejection_reason="Insufficient proof of income.",
        )
        self.make_loan(model=CashbackRequest, pk=2, disbursed=True)

        response = self.client.get(
            reverse("product-list", args=["cashback"]), {"status_group": "REJECTED"}
        )
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("REF-00001", html)
        self.assertNotIn("REF-00002", html)

    def test_in_progress_folds_the_four_mid_pipeline_statuses_together(self):
        self.make_loan(model=CashbackRequest, pk=1, reviewed=True)
        self.make_loan(model=CashbackRequest, pk=2, credit_approved=True)
        self.make_loan(model=CashbackRequest, pk=3, control_approved=True)
        self.make_loan(model=CashbackRequest, pk=4, correction_requested=True)
        self.make_loan(model=CashbackRequest, pk=5, disbursed=True)  # not "in progress"

        response = self.client.get(
            reverse("product-list", args=["cashback"]), {"status_group": "IN_PROGRESS"}
        )
        html = response.content.decode()
        for ref in ("REF-00001", "REF-00002", "REF-00003", "REF-00004"):
            self.assertIn(ref, html)
        self.assertNotIn("REF-00005", html)

    def test_clicking_through_reaches_the_rejection_reason(self):
        self.make_loan(
            model=CashbackRequest, pk=1, rejected=True,
            rejection_reason="Payslips did not match the declared employer.",
        )
        detail = self.client.get(reverse("loan-detail", args=["cashback", 1]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "Payslips did not match the declared employer.")

    def test_status_tiles_take_the_same_colour_as_their_pie_slice(self):
        """Disbursed/In progress/Rejected are both a KPI tile and a pie
        slice; they need to be the exact same colour, not just "a" green and
        "a" green that happen to look close."""
        self.make_loan(model=CashbackRequest, pk=1, disbursed=True)
        self.make_loan(model=CashbackRequest, pk=2, reviewed=True)
        self.make_loan(model=CashbackRequest, pk=3, rejected=True)

        html = self.client.get(reverse("analytics-panel"), {"product": "cashback"}).content.decode()

        for group, slice_fill in [
            ("Disbursed", "#15803D"),
            ("In progress", "#2563EB"),
            ("Rejected", "#BE123C"),
        ]:
            with self.subTest(group=group):
                self.assertIn(f'fill="{slice_fill}"', html)  # the pie slice
                self.assertIn(f'--tile-colour: {slice_fill}', html)  # the tile

    def test_applications_and_submitted_tiles_are_not_coloured(self):
        self.make_loan(model=CashbackRequest, pk=1, disbursed=True)
        html = self.client.get(reverse("analytics-panel"), {"product": "cashback"}).content.decode()
        # Only the three tiles tied to one pie slice get the modifier class
        # (the substring also appears 3 times in this page's own <style>
        # block's selectors, so count the actual class attribute, not the
        # bare word).
        self.assertEqual(html.count('class="tile tile--coloured"'), 3)


class CommissionChartTests(StatusDrilldownTestCase):
    """Cash for Car's "Commission split" card - the only product with a
    Floauto/Dash fee split, and counted on disbursed loans only."""

    def test_only_appears_for_cash_for_car(self):
        self.make_loan(model=CashbackRequest, pk=1, disbursed=True)
        for params in ({}, {"product": "cashback"}, {"product": "public-sector"}):
            with self.subTest(params=params):
                html = self.client.get(reverse("analytics-panel"), params).content.decode()
                self.assertNotIn("Commission split", html)

        html = self.client.get(reverse("analytics-panel"), {"product": "cash-for-car"}).content.decode()
        self.assertIn("Commission split", html)

    def test_counts_disbursed_loans_only(self):
        self.make_loan(model=CashForCarRequest, pk=1, disbursed=True,
                        fee_floauto_share=45_000, fee_dash_share=30_000)
        self.make_loan(model=CashForCarRequest, pk=2, disbursed=True,
                        fee_floauto_share=25_000, fee_dash_share=20_000)
        # Not disbursed - must NOT count, even though they carry fee values.
        self.make_loan(model=CashForCarRequest, pk=3, rejected=True,
                        fee_floauto_share=999_999, fee_dash_share=999_999)
        self.make_loan(model=CashForCarRequest, pk=4, loan_amount=250_000,
                        fee_floauto_share=888_888, fee_dash_share=888_888)

        html = self.client.get(reverse("analytics-panel"), {"product": "cash-for-car"}).content.decode()
        self.assertIn("&#8358;70,000", html)  # Floauto: 45,000 + 25,000
        self.assertIn("&#8358;50,000", html)  # Dash: 30,000 + 20,000
        self.assertNotIn("999,999", html)
        self.assertNotIn("888,888", html)

    def test_per_request_comparison_counts_who_took_the_higher_share(self):
        # Dash higher on two, Floauto higher on one, one tie, one missing
        # share (counts as 0, so the other party is higher).
        self.make_loan(model=CashForCarRequest, pk=1, disbursed=True,
                        fee_floauto_share=10_000, fee_dash_share=30_000)
        self.make_loan(model=CashForCarRequest, pk=2, disbursed=True,
                        fee_floauto_share=5_000, fee_dash_share=6_000)
        self.make_loan(model=CashForCarRequest, pk=3, disbursed=True,
                        fee_floauto_share=90_000, fee_dash_share=1_000)
        self.make_loan(model=CashForCarRequest, pk=4, disbursed=True,
                        fee_floauto_share=7_000, fee_dash_share=7_000)
        self.make_loan(model=CashForCarRequest, pk=5, disbursed=True,
                        fee_floauto_share=None, fee_dash_share=2_000)
        # Not disbursed: ignored.
        self.make_loan(model=CashForCarRequest, pk=6, rejected=True,
                        fee_floauto_share=1_000, fee_dash_share=50_000)

        response = self.client.get(reverse("analytics-panel"), {"product": "cash-for-car"})
        chart = response.context["commission_winner_chart"]
        counts = {row["label"]: row["value"] for row in chart["rows"]}
        self.assertEqual(counts, {"Dash higher": 3, "Floauto higher": 1, "Equal": 1})

    def test_per_request_comparison_only_for_cash_for_car(self):
        for params in ({}, {"product": "cashback"}):
            with self.subTest(params=params):
                html = self.client.get(reverse("analytics-panel"), params).content.decode()
                self.assertNotIn("Higher share per request", html)

    def test_says_it_is_scoped_to_disbursed_loans(self):
        html = self.client.get(reverse("analytics-panel"), {"product": "cash-for-car"}).content.decode()
        self.assertIn("(disbursed)", html)


class OverviewDrilldownTests(StatusDrilldownTestCase):
    def test_overview_tile_links_to_all_requests_not_one_product(self):
        response = self.client.get(reverse("analytics-panel"))
        html = response.content.decode()
        expected_url = reverse("all-requests") + "?status_group=REJECTED"
        self.assertIn(f'<a class="tile tile--coloured" href="{expected_url}"', html)

    def test_overview_drilldown_spans_every_product(self):
        self.make_loan(model=CashbackRequest, pk=1, rejected=True)
        self.make_loan(model=CashForCarRequest, pk=1, rejected=True)
        self.make_loan(model=CashbackRequest, pk=2, disbursed=True)

        response = self.client.get(reverse("all-requests"), {"status_group": "REJECTED"})
        html = response.content.decode()
        self.assertIn("REF-00001", html)  # both products use pk 1 for their rejected row
        self.assertNotIn("REF-00002", html)


class ChartURLPlumbingTests(TestCase):
    """The chart builders below the templates: a slice/row only gets a link
    when one was actually supplied, so a chart used somewhere with no
    drill-down destination doesn't grow clickable-looking dead ends."""

    def test_pie_chart_omits_url_when_none_given(self):
        from loans.charts import pie_chart

        chart = pie_chart([{"label": "Cashback", "value": 10}])
        self.assertIsNone(chart["slices"][0]["url"])

    def test_pie_chart_carries_a_url_through_per_slice(self):
        from loans.charts import pie_chart

        chart = pie_chart([{"label": "Rejected", "value": 5, "url": "/cashback/?status_group=REJECTED"}])
        self.assertEqual(chart["slices"][0]["url"], "/cashback/?status_group=REJECTED")
