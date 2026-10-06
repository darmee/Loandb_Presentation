"""Say which database the presentation is reading and what is in it.

    python manage.py presentation_status

When the presentation looks empty, this answers the first three questions in
one go: is it connected to the database you think, does that database have
applications, and do they fall inside the default date window?
"""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connections
from django.db.utils import Error as DatabaseError
from django.utils import timezone

from loans.models import PRODUCTS
from loans.views import DEFAULT_RANGE_DAYS


class Command(BaseCommand):
    help = "Show which loan database the presentation reads and the date range of its data."

    def handle(self, *args, **options):
        db = settings.DATABASES["loans"]
        mode = "SAMPLE (USE_SAMPLE_DB=True)" if settings.SAMPLE_DB else "LIVE"
        self.stdout.write(f"Mode:     {mode}")
        self.stdout.write(f"Database: {db['NAME']} on {db['HOST'] or 'localhost'}:{db['PORT']} as {db['USER']}")
        try:
            with connections["loans"].cursor() as cursor:
                cursor.execute("SELECT 1")
        except DatabaseError as exc:
            self.stdout.write(self.style.ERROR(f"Cannot connect: {exc}"))
            return

        today = timezone.localdate()
        window_start = today - timedelta(days=DEFAULT_RANGE_DAYS - 1)
        total_in_window = 0
        for code, model in PRODUCTS.items():
            table = model._meta.db_table
            try:
                with connections["loans"].cursor() as cursor:
                    cursor.execute(
                        f"SELECT COUNT(*), MIN(created_at), MAX(created_at), "
                        f"COUNT(*) FILTER (WHERE (created_at AT TIME ZONE 'Africa/Lagos')::date >= %s) "
                        f'FROM "{table}"',
                        [window_start],
                    )
                    count, first, last, recent = cursor.fetchone()
            except DatabaseError as exc:
                self.stdout.write(self.style.ERROR(f"  {table}: cannot read ({exc})"))
                continue
            total_in_window += recent
            span = (
                f"{timezone.localtime(first):%d %b %Y} to {timezone.localtime(last):%d %b %Y}"
                if count else "no rows"
            )
            self.stdout.write(f"  {model.PRODUCT_LABEL:<14} {count:>6} applications, {span}; {recent} in the last {DEFAULT_RANGE_DAYS} days")

        if total_in_window:
            self.stdout.write(self.style.SUCCESS(f"\nThe default {DEFAULT_RANGE_DAYS}-day window has data."))
        else:
            self.stdout.write(self.style.WARNING(
                f"\nNothing was submitted in the last {DEFAULT_RANGE_DAYS} days. The presentation will jump to the "
                "latest data on first load; or refresh the sample data with: python manage.py seed_sample_db"
            ))
