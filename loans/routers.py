"""Database routing that keeps the loan application database read-only.

Two guarantees:
  1. Nothing from the `loans` app is ever written.
  2. No migration ever runs against the `loans` connection, so Django cannot
     create its own tables inside the loan schema.

This is the second of three independent layers. The first is the database
grant itself (`loan_reader` holds SELECT only); the third is `managed = False`
on every model. Any one of them alone would be enough on a good day; all three
together mean a mistake in one does not become a write.
"""


class ReadOnlyDatabaseError(Exception):
    """Raised when something attempts to write to the loan database."""


class LoansRouter:
    app_label = "loans"
    db_alias = "loans"

    def db_for_read(self, model, **hints):
        if model._meta.app_label == self.app_label:
            return self.db_alias
        return None

    def db_for_write(self, model, **hints):
        if model._meta.app_label == self.app_label:
            raise ReadOnlyDatabaseError(
                f"{model.__name__} lives in the read-only loan application "
                "database. This application never writes to it."
            )
        return None

    def allow_relation(self, obj1, obj2, **hints):
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if db == self.db_alias:
            return False
        if app_label == self.app_label:
            return False
        return None
