"""Show every account officer spelling, so ALIASES can be maintained by hand.

The officer column is free text. This prints what is actually in it, grouped
the way loans/officers.py currently would, so you can see which spellings are
already merged and which are still separate people as far as the reports are
concerned.

    python manage.py list_officers
    python manage.py list_officers --unmapped
"""

from django.core.management.base import BaseCommand
from django.db.models import Count, Q, Sum

from loans.models import PRODUCTS
from loans.officers import ALIASES, canonical, normalise


class Command(BaseCommand):
    help = "List account officer name variants and how they currently group."

    def add_arguments(self, parser):
        parser.add_argument(
            "--unmapped", action="store_true",
            help="only show names that ALIASES does not cover",
        )
        parser.add_argument(
            "--min", type=int, default=0,
            help="ignore spellings used fewer than this many times",
        )

    def handle(self, *args, **options):
        mapped_keys = set()
        for name, keys in ALIASES.items():
            mapped_keys.add(normalise(name))
            mapped_keys.update(normalise(k) for k in keys)

        raw_rows = []
        for model in PRODUCTS.values():
            rows = (
                model.objects.values("account_officer")
                .annotate(
                    count=Count("id"),
                    disbursed=Count("id", filter=Q(status="DISBURSED")),
                    value=Sum("loan_amount", filter=Q(status="DISBURSED")),
                )
            )
            raw_rows.extend(rows)

        merged = {}
        for row in raw_rows:
            raw = row["account_officer"]
            entry = merged.setdefault(raw, {"count": 0, "disbursed": 0, "value": 0})
            entry["count"] += row["count"]
            entry["disbursed"] += row["disbursed"]
            entry["value"] += row["value"] or 0

        groups = {}
        for raw, entry in merged.items():
            if entry["count"] < options["min"]:
                continue
            if options["unmapped"] and normalise(raw) in mapped_keys:
                continue
            groups.setdefault(canonical(raw), []).append((raw, entry))

        if not groups:
            self.stdout.write("Nothing to show.")
            return

        for name in sorted(groups, key=lambda n: -sum(e["count"] for _r, e in groups[n])):
            variants = sorted(groups[name], key=lambda item: -item[1]["count"])
            total = sum(e["count"] for _r, e in variants)
            value = sum(e["value"] for _r, e in variants)
            flag = "" if len(variants) == 1 else f"  [{len(variants)} spellings]"
            self.stdout.write(
                self.style.SUCCESS(f"\n{name}  -  {total} applications, {value:,.2f} disbursed{flag}")
            )
            for raw, entry in variants:
                self.stdout.write(f"    {raw!r:<40} {entry['count']:>4} apps  {entry['value']:>18,.2f}")

        self.stdout.write(
            f"\n{len(groups)} officers from {len(merged)} distinct spellings."
        )
        self.stdout.write(
            "Names shown separately that are the same person belong in "
            "ALIASES in loans/officers.py."
        )
