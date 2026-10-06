"""The derived status: the SQL CASE and the Python derivation must agree.

If they ever disagreed, the presentation's counts (computed from the database
annotation) would stop reconciling with an individual application's journey
(computed from its flags), and it would look like a data fault.
"""

from itertools import product

from django.test import SimpleTestCase

from loans.models import CashbackRequest
from loans.status import STATUS_RULES, derive_status

from .base import SPINE_FLAGS, LoanDataTestCase


class StatusDerivationTests(LoanDataTestCase):
    def test_sql_and_python_agree_on_every_combination_of_flags(self):
        combos = list(product([False, True], repeat=len(SPINE_FLAGS)))
        self.assertEqual(len(combos), 64)
        for pk, combo in enumerate(combos, start=1):
            self.make_loan(pk=pk, **dict(zip(SPINE_FLAGS, combo)))
        for loan in CashbackRequest.objects.all():
            with self.subTest(pk=loan.pk):
                self.assertEqual(loan.status, derive_status(loan))


class StatusOrderTests(SimpleTestCase):
    def test_rejected_outranks_everything(self):
        self.assertEqual(STATUS_RULES[0][0], "REJECTED")

    def test_open_correction_outranks_approvals(self):
        order = [code for code, *_ in STATUS_RULES]
        self.assertLess(order.index("CORRECTION_REQUESTED"), order.index("CONTROL_APPROVED"))
