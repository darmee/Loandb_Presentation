"""The models must match the real database, column for column.

`seed_sample_db` generates the development schema FROM the models, so every
local test would pass even if a column had been renamed upstream. This test is
the counterweight: it compares the models against a snapshot of the real
schema, so drift fails a test run rather than surfacing in production.
"""

from django.test import SimpleTestCase

from loans.models import (
    Admin,
    ApprovalAuditLog,
    CashbackRequest,
    CashForCarRequest,
    LoanComment,
    PublicSectorRequest,
    RequestDocument,
)

from .schema_snapshot import INTENTIONALLY_UNMAPPED, MAPPED_TABLES

MODELS = [
    CashbackRequest,
    CashForCarRequest,
    PublicSectorRequest,
    Admin,
    RequestDocument,
    ApprovalAuditLog,
    LoanComment,
]


class SchemaCoverageTests(SimpleTestCase):
    def test_no_model_maps_a_column_the_database_lacks(self):
        for model in MODELS:
            table = model._meta.db_table
            real = set(MAPPED_TABLES[table])
            mapped = {f.attname for f in model._meta.fields}
            with self.subTest(table=table):
                self.assertEqual(
                    mapped - real,
                    set(),
                    f"{model.__name__} maps columns that do not exist in {table}",
                )

    def test_every_database_column_is_mapped_or_explicitly_excused(self):
        for model in MODELS:
            table = model._meta.db_table
            real = set(MAPPED_TABLES[table])
            mapped = {f.attname for f in model._meta.fields}
            excused = set(INTENTIONALLY_UNMAPPED.get(table, {}))
            with self.subTest(table=table):
                self.assertEqual(
                    real - mapped - excused,
                    set(),
                    f"{table} has columns {model.__name__} does not map. Either add "
                    f"them or record the reason in INTENTIONALLY_UNMAPPED.",
                )

    def test_password_hash_is_excused_rather_than_forgotten(self):
        self.assertIn("password_hash", INTENTIONALLY_UNMAPPED["admins"])
        self.assertNotIn("password_hash", {f.attname for f in Admin._meta.fields})


class RequestTypeTests(SimpleTestCase):
    """The strings the polymorphic tables use to name each product.

    Verified against the live database on 2026-09-08. Two of the three are
    abbreviations rather than the table name, and an earlier build guessed them
    wrong - which produced no error at all, just empty document and history
    lists on every Cash for Car and Public Sector application.

    These values are pinned here so changing one requires deleting a test,
    which is a deliberate enough act to prompt re-running
    scripts/verify_request_types.py against production.
    """

    def test_request_type_strings_match_the_live_database(self):
        from loans.models import (
            REQUEST_TYPE_BY_MODEL,
            CashbackRequest,
            CashForCarRequest,
            PublicSectorRequest,
        )

        self.assertEqual(
            REQUEST_TYPE_BY_MODEL,
            {
                CashbackRequest: "cashback",
                CashForCarRequest: "cfc",
                PublicSectorRequest: "psl",
            },
        )

    def test_every_product_has_a_request_type(self):
        from loans.models import PRODUCTS, REQUEST_TYPE_BY_MODEL

        for model in PRODUCTS.values():
            self.assertIn(model, REQUEST_TYPE_BY_MODEL)
