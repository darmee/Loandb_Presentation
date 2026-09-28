"""Authentication, row visibility, PII masking and document serving.

The document tests are the important ones. These files are ID scans, payslips
and bank statements; the endpoint that serves them is the only place in this
read-only application where a request can reach raw uploaded bytes.
"""

import base64

from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse

from loans.models import CashbackRequest
from loans.views import INLINE_MIME_TYPES

from .base import TINY_PNG, LoanDataTestCase


class AuthenticationTests(LoanDataTestCase):
    def test_every_page_requires_a_login(self):
        self.make_loan(pk=1)
        for url in [
            reverse("dashboard"),
            reverse("all-requests"),
            reverse("product-list", args=["cashback"]),
            reverse("loan-detail", args=["cashback", 1]),
            reverse("export-xlsx", args=["cashback"]),
            reverse("loan-document", args=["cashback", 1, 1]),
        ]:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn("/login/", response["Location"])

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
        from django.contrib.auth.models import AnonymousUser

        self.assertEqual(
            CashbackRequest.objects.visible_to(AnonymousUser()).count(), 0
        )
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


class PageTests(SignedInTestCase):
    def test_dashboard_renders(self):
        self.make_loan(pk=1, disbursed=True)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Loan applications")

    def test_dashboard_has_a_presentation_button(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'href="/presentation/"')

    def test_analytics_panel_has_status_cards_and_chart_labels(self):
        self.make_loan(pk=1, disbursed=True)
        response = self.client.get(reverse("analytics-panel"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Applications")
        self.assertContains(response, "Submitted")
        self.assertContains(response, "Disbursed")
        self.assertContains(response, "In progress")
        self.assertContains(response, "Rejected")
        self.assertContains(response, "Request type comparison")
        self.assertContains(response, "Final outcome")
        self.assertContains(response, "Loan volume by product")
        self.assertContains(response, "Applications per month")

    def test_analytics_panel_has_no_officer_assessment(self):
        self.make_loan(pk=1, disbursed=True, account_officer="Ada Officer")
        for params in ({}, {"product": "cashback"}, {"presentation": "1"}):
            with self.subTest(params=params):
                response = self.client.get(reverse("analytics-panel"), params)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "Officer assessment")
                self.assertNotContains(response, "Officer performance")
                self.assertNotContains(response, "Ada Officer")

    def test_monthly_overview_renders_its_charts(self):
        self.make_loan(pk=1, disbursed=True)
        for params in ({}, {"presentation": "1"}):
            with self.subTest(params=params):
                response = self.client.get(reverse("monthly-overview"), params)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "Applications per month")
                self.assertContains(response, "Values requested per month")
                self.assertContains(response, "Loan size distribution")
                self.assertContains(response, "Top states")

    def test_list_and_detail_render(self):
        self.make_loan(pk=1)
        self.assertEqual(
            self.client.get(reverse("product-list", args=["cashback"])).status_code, 200
        )
        response = self.client.get(reverse("loan-detail", args=["cashback", 1]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "REF-00001")

    def test_detail_shows_officer_assessment_when_recorded(self):
        self.make_loan(pk=1)
        with self.client.login(username="clerk", password="not-used"):
            pass
        from django.db import connections

        with connections["loans"].cursor() as cursor:
            cursor.execute(
                'INSERT INTO "admins" ("id", "full_name", "email", "role", "is_active", "must_change_password", "created_at", "updated_at") VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())',
                (1, "Ada Officer", "ada@example.com", "reviewer", True, False),
            )
            cursor.execute(
                'INSERT INTO "loan_comment" ("request_type", "request_id", "author_id", "body", "is_recommendation", "created_at") VALUES (%s, %s, %s, %s, %s, NOW())',
                ("cashback", 1, 1, "Customer is a strong fit, with stable income and good repayment behaviour.", True),
            )

        response = self.client.get(reverse("loan-detail", args=["cashback", 1]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Officer assessment")
        self.assertContains(response, "Customer is a strong fit")

    def test_presentation_has_five_live_slides(self):
        response = self.client.get(reverse("presentation"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["slides"]), 5)
        self.assertEqual(
            [slide["title"] for slide in response.context["slides"]],
            ["Analytics Panel", "Monthly Overview", "Cashback", "Cash for Car", "Public Sector"],
        )

    def test_presentation_moves_on_every_five_seconds(self):
        # The interval lives in the static script, not in the page.
        from pathlib import Path

        from django.conf import settings

        script = (Path(settings.BASE_DIR) / "static" / "js" / "presentation.js").read_text()
        self.assertIn("SLIDE_INTERVAL = 5000", script)

    def test_product_page_hides_the_loan_volume_by_product_chart(self):
        self.make_loan(pk=1, disbursed=True)
        response = self.client.get(reverse("analytics-panel"), {"product": "cashback"})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Loan volume by product")
        self.assertContains(response, "Final outcome")
        self.assertContains(response, "Applications per month")
        self.assertContains(response, "Loan size distribution")
        self.assertContains(response, "Top states")

    def test_unified_list_renders_every_product(self):
        from loans.models import CashForCarRequest, PublicSectorRequest

        self.make_loan(model=CashbackRequest, pk=1)
        self.make_loan(model=CashForCarRequest, pk=1)
        self.make_loan(model=PublicSectorRequest, pk=1)
        response = self.client.get(reverse("all-requests"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cashback")
        self.assertContains(response, "Cash for Car")
        self.assertContains(response, "Public Sector")

    def test_unknown_product_is_404_not_500(self):
        self.assertEqual(
            self.client.get(reverse("product-list", args=["mortgages"])).status_code, 404
        )

    def test_status_filter_narrows_the_list(self):
        self.make_loan(pk=1, disbursed=True)
        self.make_loan(pk=2)
        url = reverse("product-list", args=["cashback"])
        self.assertContains(self.client.get(url, {"status": "DISBURSED"}), "REF-00001")
        self.assertNotContains(self.client.get(url, {"status": "DISBURSED"}), "REF-00002")

    def test_export_returns_a_workbook(self):
        self.make_loan(pk=1)
        response = self.client.get(reverse("export-xlsx", args=["cashback"]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("spreadsheetml", response["Content-Type"])
        self.assertIn("attachment", response["Content-Disposition"])


class PiiMaskingTests(SignedInTestCase):
    @override_settings(EXPORT_INCLUDE_PII=False)
    def test_bvn_is_masked_on_the_detail_page_by_default(self):
        self.make_loan(pk=1, bvn="22233344455")
        response = self.client.get(reverse("loan-detail", args=["cashback", 1]))
        self.assertNotContains(response, "22233344455")

    @override_settings(EXPORT_INCLUDE_PII=True)
    def test_bvn_is_shown_when_pii_is_deliberately_enabled(self):
        self.make_loan(pk=1, bvn="22233344455")
        response = self.client.get(reverse("loan-detail", args=["cashback", 1]))
        self.assertContains(response, "22233344455")


class DocumentTests(SignedInTestCase):
    def test_png_is_served_inline_and_decoded(self):
        self.make_loan(pk=1)
        self.make_document(request_id=1, doc_id=1, mime_type="image/png")
        response = self.client.get(reverse("loan-document", args=["cashback", 1, 1]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertIn("inline", response["Content-Disposition"])
        self.assertEqual(response.content, base64.b64decode(TINY_PNG))

    def test_html_upload_is_forced_to_download(self):
        """An uploaded HTML file served inline would be stored XSS.

        mime_type is whatever the upload set, so it cannot be trusted. Anything
        outside the inline whitelist must download as an opaque blob.
        """
        self.make_loan(pk=1)
        payload = base64.b64encode(b"<script>alert(document.cookie)</script>").decode()
        self.make_document(request_id=1, doc_id=1, mime_type="text/html", data=payload)
        response = self.client.get(reverse("loan-document", args=["cashback", 1, 1]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/octet-stream")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_html_is_not_in_the_inline_whitelist(self):
        self.assertNotIn("text/html", INLINE_MIME_TYPES)
        self.assertNotIn("image/svg+xml", INLINE_MIME_TYPES)

    def test_document_cannot_be_fetched_through_another_application(self):
        """The parent application governs access, not the document id alone."""
        self.make_loan(pk=1)
        self.make_loan(pk=2)
        self.make_document(request_id=2, doc_id=7)
        # Document 7 belongs to application 2. Asking for it under 1 must fail.
        response = self.client.get(reverse("loan-document", args=["cashback", 1, 7]))
        self.assertEqual(response.status_code, 404)

    def test_document_cannot_be_fetched_through_another_product(self):
        from loans.models import PublicSectorRequest

        self.make_loan(model=CashbackRequest, pk=1)
        self.make_loan(model=PublicSectorRequest, pk=1)
        self.make_document(request_type="cashback", request_id=1, doc_id=3)
        response = self.client.get(reverse("loan-document", args=["public-sector", 1, 3]))
        self.assertEqual(response.status_code, 404)

    def test_corrupt_base64_is_a_404_not_a_500(self):
        self.make_loan(pk=1)
        self.make_document(request_id=1, doc_id=1, data="!!!not base64!!!")
        response = self.client.get(reverse("loan-document", args=["cashback", 1, 1]))
        self.assertEqual(response.status_code, 404)

    def test_every_document_access_is_audited(self):
        self.make_loan(pk=1)
        self.make_document(request_id=1, doc_id=1)
        with self.assertLogs("loans.audit", level="INFO") as captured:
            self.client.get(reverse("loan-document", args=["cashback", 1, 1]))
        line = "\n".join(captured.output)
        self.assertIn("DOCUMENT", line)
        self.assertIn("user=clerk", line)
        self.assertIn("ref=REF-00001", line)

    def test_export_is_audited_with_row_count_and_pii_state(self):
        self.make_loan(pk=1)
        with self.assertLogs("loans.audit", level="INFO") as captured:
            self.client.get(reverse("export-xlsx", args=["cashback"]))
        line = "\n".join(captured.output)
        self.assertIn("EXPORT", line)
        self.assertIn("rows=1", line)
        self.assertIn("pii=masked", line)


class ExportContentTests(SignedInTestCase):
    def test_export_includes_the_derived_status_column(self):
        """Status is an annotation, not a field - it has to be added by hand."""
        from loans.views import _export_columns

        columns = _export_columns(CashbackRequest)
        self.assertIn("status", columns)
        self.assertLess(columns.index("status"), columns.index("created_at"))

    def test_export_resolves_actor_ids_to_names(self):
        from loans.views import _export_columns

        columns = _export_columns(CashbackRequest)
        self.assertIn("reviewed_by__full_name", columns)
        self.assertNotIn("reviewed_by", columns)

    def test_export_timestamps_are_local_and_naive(self):
        """Excel rejects aware datetimes, and UTC would be an hour out."""
        from datetime import datetime, timezone as dt_timezone

        from loans.views import _naive_local

        aware = datetime(2026, 3, 1, 23, 30, tzinfo=dt_timezone.utc)
        local = _naive_local(aware)
        self.assertIsNone(local.tzinfo)
        # Africa/Lagos is UTC+1, so 23:30 UTC is 00:30 the following day.
        self.assertEqual(local.hour, 0)
        self.assertEqual(local.day, 2)


class FilenameSanitisationTests(SignedInTestCase):
    """`file_name` comes from the uploader, so it must never reach a header raw."""

    def test_quotes_cannot_break_out_of_the_header(self):
        self.make_loan(pk=1)
        self.make_document(
            request_id=1, doc_id=1,
            file_name='evil".pdf"; x=y; download="pwned.exe',
        )
        response = self.client.get(reverse("loan-document", args=["cashback", 1, 1]))
        header = response["Content-Disposition"]

        # One quoted filename parameter, and the quoted value itself contains
        # none of the characters that would end it early or add parameters.
        self.assertEqual(header.count('filename="'), 1)
        quoted = header.split('filename="', 1)[1].split('"', 1)[0]
        for char in ('"', ";", "=", "\\", "/"):
            self.assertNotIn(char, quoted, f"{char!r} survived into the header")
        # The injected parameters are inert text inside the filename, not
        # parameters of their own.
        self.assertNotIn("x=y", header)
        self.assertNotIn("download=", header)

    def test_path_separators_are_stripped(self):
        from loans.views import _safe_filename

        self.assertNotIn("/", _safe_filename("../../etc/passwd"))
        self.assertNotIn("\\", _safe_filename(r"..\..\windows\system32"))

    def test_empty_or_hostile_name_falls_back(self):
        from loans.views import _safe_filename

        self.assertEqual(_safe_filename(""), "document")
        self.assertEqual(_safe_filename(None), "document")
        self.assertEqual(_safe_filename("///"), "___")


class SecurityHeaderTests(SignedInTestCase):
    def test_scripts_are_forbidden_outright(self):
        """The app ships no JavaScript, so script-src can be 'none'."""
        self.make_loan(pk=1)
        response = self.client.get(reverse("product-list", args=["cashback"]))
        csp = response["Content-Security-Policy"]
        self.assertIn("script-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("object-src 'none'", csp)

    def test_presentation_requests_allow_own_iframe_and_script(self):
        response = self.client.get(reverse("presentation"))
        csp = response["Content-Security-Policy"]
        self.assertIn("script-src 'self' 'unsafe-inline'", csp)
        self.assertIn("frame-ancestors 'self'", csp)
        self.assertEqual(response["X-Frame-Options"], "SAMEORIGIN")

    def test_served_documents_are_sandboxed(self):
        self.make_loan(pk=1)
        self.make_document(request_id=1, doc_id=1)
        response = self.client.get(reverse("loan-document", args=["cashback", 1, 1]))
        self.assertIn("sandbox", response["Content-Security-Policy"])

    def test_standard_hardening_headers_are_present(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response["X-Frame-Options"], "DENY")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        self.assertIn("noindex", response["X-Robots-Tag"])
        self.assertIn("camera=()", response["Permissions-Policy"])
