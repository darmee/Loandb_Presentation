"""Authentication, row visibility, routing and security headers.

The application is now a single presentation page plus the JSON it draws
from. Every one of those endpoints must refuse an anonymous caller, and the
page must be what a signed-in user lands on.
"""

from django.contrib.auth.models import AnonymousUser, User
from django.urls import reverse

from loans.models import CashbackRequest

from .base import LoanDataTestCase

API_URLS = [
    ("api-dashboard", []),
    ("api-day", []),
    ("api-drill", []),
    ("api-compare", []),
    ("api-search", []),
    ("api-journey", ["cashback", 1]),
]


class AuthenticationTests(LoanDataTestCase):
    def test_the_presentation_requires_a_login(self):
        response = self.client.get(reverse("presentation"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_every_api_endpoint_refuses_an_anonymous_caller(self):
        self.make_loan(pk=1)
        for name, args in API_URLS:
            with self.subTest(endpoint=name):
                response = self.client.get(reverse(name, args=args))
                # 401 JSON, not a redirect: fetch() would follow a redirect to
                # the HTML sign-in page and fail somewhere confusing.
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response["Content-Type"], "application/json")

    def test_signing_in_lands_on_the_presentation(self):
        User.objects.create_user("clerk", password="a-long-test-password")
        response = self.client.post(
            reverse("login"), {"username": "clerk", "password": "a-long-test-password"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("presentation"))
        self.assertEqual(reverse("presentation"), "/")

    def test_healthz_needs_no_login(self):
        response = self.client.get(reverse("healthz"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["app"], "ok")

    def test_healthz_reveals_nothing_about_the_data(self):
        body = self.client.get(reverse("healthz")).json()
        self.assertEqual(set(body), {"app", "database"})


class VisibilityTests(LoanDataTestCase):
    def test_anonymous_user_sees_no_rows(self):
        self.make_loan(pk=1)
        self.assertEqual(CashbackRequest.objects.visible_to(AnonymousUser()).count(), 0)
        self.assertEqual(CashbackRequest.objects.visible_to(None).count(), 0)

    def test_authenticated_user_sees_every_row_today(self):
        self.make_loan(pk=1)
        self.make_loan(pk=2)
        user = User.objects.create_user("clerk", password="x")
        self.assertEqual(CashbackRequest.objects.visible_to(user).count(), 2)


class SignedInTestCase(LoanDataTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user("clerk", password="not-used")
        self.client.force_login(self.user)


class PresentationPageTests(SignedInTestCase):
    def test_renders_every_tab(self):
        response = self.client.get(reverse("presentation"))
        self.assertEqual(response.status_code, 200)
        for tab in ["overview", "monthly", "product-cashback", "product-cash-for-car", "product-public-sector",
                    "daily", "flow", "compare", "insights"]:
            self.assertContains(response, f'data-tab="{tab}"')
        # Folded away at the user's request: too much for one deck.
        for gone in ["funnel", "rejections", "corrections", "journey"]:
            self.assertNotContains(response, f'data-tab="{gone}"')

    def test_deck_pages_have_four_charts_each(self):
        html = self.client.get(reverse("presentation")).content.decode()
        for panel in ["overview", "monthly", "product-cashback", "product-cash-for-car", "product-public-sector"]:
            with self.subTest(panel=panel):
                section = html.split(f'id="panel-{panel}"', 1)[1].split("</section>", 1)[0]
                self.assertEqual(section.count('class="chart"'), 4)
                self.assertIn("g2x2", section)

    def test_insights_are_the_last_page(self):
        html = self.client.get(reverse("presentation")).content.decode()
        tabs = [chunk.split('"', 1)[0] for chunk in html.split('data-tab="')[1:]]
        self.assertEqual(tabs[-1], "insights")

    def test_old_presentation_url_redirects_to_the_new_page(self):
        response = self.client.get("/presentation/")
        self.assertRedirects(response, reverse("presentation"), fetch_redirect_response=False)

    def test_old_dashboard_pages_are_gone(self):
        for url in ["/all/", "/analytics/", "/analytics/panel/", "/cashback/"]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_ships_no_inline_script(self):
        html = self.client.get(reverse("presentation")).content.decode()
        # The only <script> without src is json_script's data block, which
        # the browser never executes.
        for chunk in html.split("<script")[1:]:
            head = chunk.split(">", 1)[0]
            self.assertTrue("src=" in head or 'type="application/json"' in head, head)

    def test_content_security_policy_forbids_inline_and_foreign_scripts(self):
        csp = self.client.get(reverse("presentation"))["Content-Security-Policy"]
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe-inline' ;", csp)
        script = [d for d in csp.split(";") if d.strip().startswith("script-src")][0]
        self.assertNotIn("unsafe", script)
        self.assertIn("frame-ancestors 'none'", csp)

    def test_every_response_is_marked_noindex(self):
        for url in [reverse("presentation"), reverse("api-dashboard"), reverse("login")]:
            with self.subTest(url=url):
                self.assertIn("noindex", self.client.get(url)["X-Robots-Tag"])


class ApiTests(SignedInTestCase):
    def test_dashboard_shows_every_request_by_default(self):
        old = "2001-03-04T10:00:00+00:00"
        self.make_loan(pk=1, reviewed=True, credit_approved=True, control_approved=True, disbursed=True,
                       created_at=old, updated_at=old)
        self.make_loan(pk=2, reviewed=True, rejected=True, rejection_reason="Affordability below threshold.")
        self.make_loan(pk=3)
        data = self.client.get(reverse("api-dashboard")).json()
        tiles = data["scopes"]["all"]["tiles"]
        # "All time" really is all time: the 2001 application counts too.
        self.assertTrue(data["window"]["all"])
        self.assertEqual(tiles["applications"], 3)
        self.assertEqual((tiles["disbursed"], tiles["rejected"], tiles["pending"]), (1, 1, 1))
        outcome = {o["key"]: o["count"] for o in data["scopes"]["all"]["outcome"]}
        self.assertEqual(sum(outcome.values()), tiles["applications"])

    def test_dashboard_has_a_block_per_product_and_the_dash_floauto_split(self):
        self.make_loan(pk=1)
        data = self.client.get(reverse("api-dashboard")).json()
        self.assertEqual(set(data["scopes"]), {"all", "cashback", "cash-for-car", "public-sector"})
        self.assertEqual([p["key"] for p in data["products"]], ["cashback", "cash-for-car", "public-sector"])
        self.assertEqual(set(data["commission"]), {"loans", "dash", "floauto", "dash_higher", "floauto_higher", "equal"})
        for block in data["scopes"].values():
            self.assertEqual(set(block), {"tiles", "outcome", "statuses", "monthly", "bands", "states", "officers"})

    def test_dash_floauto_split_counts_disbursed_cash_for_car_only(self):
        from loans.models import CashForCarRequest
        self.make_loan(CashForCarRequest, pk=1, reviewed=True, credit_approved=True, control_approved=True,
                       disbursed=True, fee_dash_share=300, fee_floauto_share=100)
        self.make_loan(CashForCarRequest, pk=2, reviewed=True, credit_approved=True, control_approved=True,
                       disbursed=True, fee_dash_share=50, fee_floauto_share=200)
        self.make_loan(CashForCarRequest, pk=3, fee_dash_share=999, fee_floauto_share=999)   # not disbursed
        c = self.client.get(reverse("api-dashboard")).json()["commission"]
        self.assertEqual((c["loans"], c["dash"], c["floauto"]), (2, 350.0, 300.0))
        self.assertEqual((c["dash_higher"], c["floauto_higher"], c["equal"]), (1, 1, 0))

    def test_rejected_drill_carries_the_reason(self):
        for pk in (1, 2):
            self.make_loan(pk=pk, reviewed=True, rejected=True, rejection_reason="Adverse credit bureau record.")
        self.make_loan(pk=3)
        data = self.client.get(reverse("api-drill"), {"period": "all", "kind": "group", "key": "REJECTED"}).json()
        self.assertEqual(data["count"], 2)
        self.assertEqual({r["reason"] for r in data["rows"]}, {"Adverse credit bureau record."})

    def test_drill_list_matches_the_number_on_the_chart(self):
        for pk in (1, 2, 3):
            self.make_loan(pk=pk, reviewed=True)
        self.make_loan(pk=4)
        block = self.client.get(reverse("api-dashboard")).json()["scopes"]["cashback"]
        in_progress = [o for o in block["outcome"] if o["key"] == "IN_PROGRESS"][0]["count"]
        drill = self.client.get(reverse("api-drill"), {"period": "all", "kind": "group", "key": "IN_PROGRESS",
                                                       "product": "cashback"}).json()
        self.assertEqual(in_progress, 3)
        self.assertEqual(drill["count"], 3)

    def test_an_empty_window_still_says_where_the_data_is(self):
        self.make_loan(pk=1)
        data = self.client.get(reverse("api-dashboard"), {"period": "range", "start": "2000-01-01", "end": "2000-01-31"}).json()
        self.assertEqual(data["scopes"]["all"]["tiles"]["applications"], 0)
        self.assertEqual(data["data"]["total"], 1)
        self.assertIn("name", data["database"])

    def test_assets_carry_a_cache_busting_version(self):
        html = self.client.get(reverse("presentation")).content.decode()
        self.assertRegex(html, r"js/presentation\.js\?v=\d+")

    def test_bad_date_is_a_400_not_a_500(self):
        response = self.client.get(reverse("api-dashboard"), {"period": "range", "start": "yesterday"})
        self.assertEqual(response.status_code, 400)

    def test_unknown_product_is_a_400(self):
        response = self.client.get(reverse("api-drill"), {"kind": "all", "key": "1", "product": "mortgages"})
        self.assertEqual(response.status_code, 400)

    def test_unknown_drill_is_a_400(self):
        response = self.client.get(reverse("api-drill"), {"kind": "nope"})
        self.assertEqual(response.status_code, 400)

    def test_search_finds_by_reference_and_journey_opens(self):
        self.make_loan(pk=7, reviewed=True)
        rows = self.client.get(reverse("api-search"), {"q": "REF-00007"}).json()["rows"]
        self.assertEqual([r["reference"] for r in rows], ["REF-00007"])
        journey = self.client.get(reverse("api-journey", args=["cashback", 7])).json()
        self.assertEqual([s["state"] for s in journey["steps"]], ["done", "done", "current", "future", "future"])

    def test_journey_of_a_missing_application_is_404(self):
        response = self.client.get(reverse("api-journey", args=["cashback", 999]))
        self.assertEqual(response.status_code, 404)

    def test_journey_does_not_expose_identity_numbers(self):
        self.make_loan(pk=1)
        body = self.client.get(reverse("api-journey", args=["cashback", 1])).content.decode()
        self.assertNotIn("12345678901", body)   # BVN from make_loan
        self.assertNotIn("10987654321", body)   # NIN
