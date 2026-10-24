"""Write the period report: the last three months (or any dates) against the period before.

    python manage.py period_report                         # the last 3 complete months
    python manage.py period_report --months 6              # the last 6 complete months
    python manage.py period_report --start 2026-07-01 --end 2026-09-30
    python manage.py period_report --pdf                   # also save a PDF (needs Microsoft Edge)

The report is written to reports/ (not committed: it holds business figures).
Open the .html in a browser to read it, or print it (Ctrl+P -> Save as PDF).
"""

import shutil
import subprocess
from datetime import date
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from loans import report

EDGE = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


class Command(BaseCommand):
    help = "Write the period report (last 3 complete months by default) to reports/."

    def add_arguments(self, parser):
        parser.add_argument("--months", type=int, default=3, help="Complete months to cover (default 3).")
        parser.add_argument("--start", type=date.fromisoformat, help="First day, YYYY-MM-DD (with --end).")
        parser.add_argument("--end", type=date.fromisoformat, help="Last day, YYYY-MM-DD (with --start).")
        parser.add_argument("--out", default=str(Path(settings.BASE_DIR) / "reports"), help="Folder to write to.")
        parser.add_argument("--pdf", action="store_true", help="Also save a PDF, using Microsoft Edge.")

    def handle(self, months, start, end, out, pdf, **options):
        if bool(start) != bool(end):
            raise CommandError("Give both --start and --end, or neither.")
        if start and start > end:
            raise CommandError("--start must be on or before --end.")
        if not start:
            if months < 1:
                raise CommandError("--months must be at least 1.")
            start, end = report.default_period(months, timezone.localdate())

        folder = Path(out)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"loan-report-{start}-to-{end}.html"
        path.write_text(report.render(start, end), encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Report for {start} to {end}: {path}"))

        if pdf:
            edge = next((e for e in EDGE if Path(e).exists()), shutil.which("msedge"))
            if not edge:
                self.stdout.write("Microsoft Edge was not found: open the .html and print it to PDF (Ctrl+P).")
                return
            pdf_path = path.with_suffix(".pdf")
            subprocess.run([edge, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                            f"--print-to-pdf={pdf_path}", path.resolve().as_uri()],
                           check=False, timeout=120, capture_output=True)
            if pdf_path.exists():
                self.stdout.write(self.style.SUCCESS(f"PDF: {pdf_path}"))
            else:
                self.stdout.write("The PDF could not be made: open the .html and print it to PDF (Ctrl+P).")
