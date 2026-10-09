"""Authentication, row visibility, routing and security headers.

The application is now a single presentation page plus the JSON it draws
from. Every one of those endpoints must refuse an anonymous caller, and the
page must be what a signed-in user lands on.
"""

from datetime import datetime, timedelta, timezone as dt_timezone

from django.contrib.auth.models import AnonymousUser, User
from django.db import connections
from django.urls import reverse

from loans.models import CashbackRequest

from .base import LoanDataTestCase

API_URLS = [
    ("api-dashboard", []),
    ("api-day", []),
    ("api-live", []),
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

    def test_every_page_in_the_rail_carries_its_full_name(self):
        # The rail folds to icons on a narrow screen and may shorten a label,
        # so the full name lives in `title`: it is the tooltip, and what the
        # insight cards and the command palette call the page.
        html = self.client.get(reverse("presentation")).content.decode()
        for name in ["Overview", "Daily report", "Monthly overview", "Cashback", "Cash for Car", "Public Sector",
                     "Applications vs disbursements", "Compare periods", "Top insights"]:
            with self.subTest(page=name):
                self.assertIn(f'title="{name}"', html)

    def test_the_command_palette_ships_closed_and_out_of_reach(self):
        html = self.client.get(reverse("presentation")).content.decode()
        self.assertIn('id="search-open"', html)
        palette = html.split('id="palette"', 1)[1].split(">", 1)[0]
        self.assertIn("inert", palette)
        self.assertIn('aria-modal="true"', palette)

    def test_the_daily_report_is_the_live_feed_and_the_day_by_day_table(self):
        html = self.client.get(reverse("presentation")).content.decode()
        tabs = [chunk.split('"', 1)[0] for chunk in html.split('data-tab="')[1:]]
        self.assertEqual(tabs[:2], ["overview", "daily"])   # the second page
        section = html.split('id="panel-daily"', 1)[1].split("</section>", 1)[0]
        self.assertEqual(section.count('class="card"'), 2)   # those two things, and nothing else
        self.assertIn('id="live-feed"', section)
        self.assertIn('id="daily-table"', section)

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

    def test_animation_and_scroll_libraries_are_served_from_this_origin(self):
        # Motion and Lenis are vendored: script-src 'self' would block a CDN.
        html = self.client.get(reverse("presentation")).content.decode()
        for script in ["vendor/motion/motion.min.js", "vendor/lenis/lenis.min.js", "js/ui.js"]:
            with self.subTest(script=script):
                self.assertIn(script, html)
        self.assertNotIn("://", "".join(c.split(">", 1)[0] for c in html.split("<script")[1:]))

    def test_vendored_scripts_point_at_no_missing_source_map(self):
        # collectstatic's manifest storage resolves every sourceMappingURL it
        # finds and aborts the deployment when the .map file is not there.
        from pathlib import Path
        from django.conf import settings
        for script in Path(settings.BASE_DIR, "static").rglob("*.js"):
            with self.subTest(script=script.name):
                for line in script.read_text(encoding="utf-8").splitlines():
                    if "sourceMappingURL=" in line:
                        target = line.split("sourceMappingURL=", 1)[1].strip()
                        self.assertTrue((script.parent / target).exists(), f"{script.name} -> {target}")

    def test_the_page_is_never_stored_by_the_browser(self):
        # Otherwise the Back button shows it again after signing out.
        self.assertIn("no-store", self.client.get(reverse("presentation"))["Cache-Control"])

    def test_a_signed_in_user_is_not_shown_the_sign_in_form(self):
        response = self.client.get(reverse("login"))
        self.assertRedirects(response, reverse("presentation"), fetch_redirect_response=False)

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

    def test_monthly_rows_account_for_every_request_in_the_month(self):
        # These feed the trend line behind each headline tile, so a month's
        # states must add up to the month's requests, like the tiles do.
        self.make_loan(pk=1, reviewed=True, credit_approved=True, control_approved=True, disbursed=True)
        self.make_loan(pk=2, reviewed=True)
        self.make_loan(pk=3, reviewed=True, rejected=True)
        self.make_loan(pk=4)
        block = self.client.get(reverse("api-dashboard")).json()["scopes"]["all"]
        for key in ("count", "disbursed", "in_progress", "pending", "rejected"):
            with self.subTest(key=key):
                self.assertEqual(sum(m[key] for m in block["monthly"]),
                                 {"count": 4, "disbursed": 1, "in_progress": 1, "pending": 1, "rejected": 1}[key])
        for month in block["monthly"]:
            self.assertEqual(month["count"], month["disbursed"] + month["in_progress"] + month["pending"] + month["rejected"])

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

    def test_api_answers_are_never_stored_by_the_browser(self):
        # They carry applicant names; a cached copy would outlive the session.
        self.make_loan(pk=1)
        for name, args in API_URLS:
            with self.subTest(endpoint=name):
                self.assertEqual(self.client.get(reverse(name, args=args))["Cache-Control"], "no-store")
        self.client.logout()
        self.assertEqual(self.client.get(reverse("api-dashboard"))["Cache-Control"], "no-store")

    def test_api_only_reads(self):
        for name, args in API_URLS:
            with self.subTest(endpoint=name):
                self.assertEqual(self.client.post(reverse(name, args=args)).status_code, 405)

    def test_dashboard_says_what_today_is(self):
        # A deck left open overnight learns the date has changed from this.
        from django.utils import timezone
        data = self.client.get(reverse("api-dashboard")).json()
        self.assertEqual(data["today"], timezone.localdate().isoformat())

    def test_journey_of_an_unknown_product_is_a_json_404(self):
        response = self.client.get(reverse("api-journey", args=["mortgages", 1]))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/json")

    # --- the live feed -------------------------------------------------------

    def _live_fixture(self):
        """Two loans: one submitted an hour ago and reviewed five minutes ago,
        one submitted a minute ago. Returns the moments, oldest first."""
        now = datetime.now(dt_timezone.utc).replace(microsecond=0)
        hour, five, one = now - timedelta(hours=1), now - timedelta(minutes=5), now - timedelta(minutes=1)
        self.make_loan(pk=1, created_at=hour, updated_at=five, reviewed=True, reviewed_at=five)
        self.make_loan(pk=2, created_at=one, updated_at=one)
        return hour, five, one

    def test_live_feed_lists_what_happened_newest_first(self):
        hour, five, one = self._live_fixture()
        data = self.client.get(reverse("api-live")).json()
        self.assertEqual([(e["reference"], e["event"]) for e in data["events"]],
                         [("REF-00002", "submitted"), ("REF-00001", "reviewed"), ("REF-00001", "submitted")])
        self.assertEqual(datetime.fromisoformat(data["latest"]), one)
        self.assertEqual({e["name"] for e in data["events"]}, {"Test Applicant"})
        self.assertEqual(len({e["key"] for e in data["events"]}), 3)

    def test_live_feed_since_returns_only_what_is_newer(self):
        hour, five, one = self._live_fixture()
        url = reverse("api-live")
        newer = self.client.get(url, {"since": (five - timedelta(seconds=1)).isoformat()}).json()
        self.assertEqual([e["event"] for e in newer["events"]], ["submitted", "reviewed"])
        # Asking from the newest moment itself: nothing more to say.
        nothing = self.client.get(url, {"since": one.isoformat()}).json()
        self.assertEqual((nothing["events"], nothing["latest"]), ([], None))

    def test_live_feed_ignores_a_timestamp_whose_step_was_undone(self):
        # reviewed_at left behind, but the flag is off: not an event.
        now = datetime.now(dt_timezone.utc)
        self.make_loan(pk=1, created_at=now - timedelta(hours=2), reviewed=False, reviewed_at=now)
        events = self.client.get(reverse("api-live")).json()["events"]
        self.assertEqual([e["event"] for e in events], ["submitted"])

    def test_live_feed_says_who_and_why(self):
        from loans.models import Admin
        now = datetime.now(dt_timezone.utc)
        with connections["loans"].cursor() as cursor:
            cursor.execute(
                'INSERT INTO "admins" (id, full_name, email, role, is_active, must_change_password, created_at, updated_at) '
                "VALUES (9, 'Kola Adeniyi', 'k@example.com', 'reviewer', true, false, %s, %s)", [now, now])
        self.assertEqual(Admin.objects.count(), 1)
        self.make_loan(pk=1, created_at=now - timedelta(hours=3), reviewed=True, reviewed_at=now - timedelta(hours=2),
                       rejected=True, rejected_at=now - timedelta(hours=1), rejected_by_id=9,
                       rejection_reason="Adverse credit bureau record.")
        newest = self.client.get(reverse("api-live")).json()["events"][0]
        self.assertEqual((newest["event"], newest["by"], newest["detail"], newest["status"]),
                         ("rejected", "Kola Adeniyi", "Adverse credit bureau record.", "REJECTED"))

    def test_live_feed_can_be_narrowed_to_a_product_and_a_length(self):
        from loans.models import CashForCarRequest
        self._live_fixture()
        self.make_loan(CashForCarRequest, pk=5)
        url = reverse("api-live")
        self.assertEqual({e["product"] for e in self.client.get(url, {"product": "cash-for-car"}).json()["events"]}, {"cash-for-car"})
        self.assertEqual(len(self.client.get(url, {"limit": "2"}).json()["events"]), 2)

    def test_live_feed_does_not_expose_identity_numbers(self):
        self._live_fixture()
        body = self.client.get(reverse("api-live")).content.decode()
        self.assertNotIn("12345678901", body)   # BVN from make_loan
        self.assertNotIn("10987654321", body)   # NIN

    def test_live_feed_refuses_a_since_it_cannot_place_in_time(self):
        url = reverse("api-live")
        for bad in ["yesterday", "2026-10-08T09:30:00"]:   # not a time; a time with no zone
            with self.subTest(since=bad):
                self.assertEqual(self.client.get(url, {"since": bad}).status_code, 400)
        self.assertEqual(self.client.get(url, {"limit": "lots"}).status_code, 400)

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
