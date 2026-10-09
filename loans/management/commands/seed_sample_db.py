"""Build a local PostgreSQL copy holding fabricated loan applications.

The real database listens on loopback on the Windows server, so day-to-day
development needs something local. This creates `dash_loans_dev` with the same
table shapes and fills it with invented people.

PostgreSQL rather than SQLite on purpose: this application depends on
timezone-aware timestamps, `numeric` precision and CASE/WHEN ordering, and
SQLite reproduces none of those faithfully. Developing against a different
engine than production is how "works locally" becomes "breaks on the server".

The schema is generated from the models via Django's schema editor, so it
cannot drift from what the application expects. Note the limitation that
follows from this: it verifies the app against the models, NOT the models
against the real database. For that, run scripts/verify_schema.py against the
live database, which compares the two column by column.

Refuses to run unless USE_SAMPLE_DB=True, so it can never touch production.
"""

import random
from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from loans.models import (
    REQUEST_TYPE_BY_MODEL,
    Admin,
    ApprovalAuditLog,
    CashbackRequest,
    CashForCarRequest,
    LoanComment,
    PublicSectorRequest,
    RequestDocument,
)

FIRST_NAMES = [
    "Adaeze", "Chinedu", "Ngozi", "Emeka", "Folake", "Tunde", "Amaka", "Segun",
    "Halima", "Ibrahim", "Yetunde", "Musa", "Blessing", "Obinna", "Zainab",
    "Kelechi", "Aisha", "Femi", "Chiamaka", "Suleiman", "Temitope", "Uche",
]
LAST_NAMES = [
    "Okonkwo", "Adeyemi", "Bello", "Eze", "Ibrahim", "Okafor", "Balogun",
    "Nwosu", "Abubakar", "Oyelaran", "Chukwu", "Danjuma", "Adebayo", "Musa",
    "Olawale", "Nnamdi", "Yusuf", "Ogundipe", "Umeh", "Lawal",
]
STATES = [
    "Lagos", "Ogun", "Oyo", "Kano", "Rivers", "Abuja FCT", "Enugu", "Kaduna",
    "Anambra", "Delta", "Edo", "Plateau",
]
OCCUPATIONS = ["Trader", "Teacher", "Civil servant", "Engineer", "Nurse",
               "Accountant", "Driver", "Farmer", "Software developer"]
OFFICERS = ["A. Salami", "B. Okoro", "C. Adekunle", "D. Mohammed", "E. Nwachukwu"]
BANKS = ["Dash MFB", "GTBank", "Zenith Bank", "Access Bank", "UBA", "First Bank"]
MDAS = ["Ministry of Education", "Ministry of Health", "Federal Inland Revenue",
        "Ministry of Works", "National Assembly Service"]
DOC_TYPES = ["ID_CARD", "BANK_STATEMENT", "UTILITY_BILL", "PASSPORT_PHOTO",
             "EMPLOYMENT_LETTER", "PAYSLIP"]
VEHICLES = [("Toyota", "Corolla"), ("Honda", "Accord"), ("Lexus", "RX 350"),
            ("Mercedes-Benz", "C300"), ("Kia", "Sportage")]


TINY_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
    "IQAAAABJRU5ErkJggg=="
)

MODELS = [
    Admin,
    CashbackRequest,
    CashForCarRequest,
    PublicSectorRequest,
    RequestDocument,
    ApprovalAuditLog,
    LoanComment,
]


class Command(BaseCommand):
    help = "Create and populate a local PostgreSQL database with fabricated loans."

    def add_arguments(self, parser):
        parser.add_argument("--cashback", type=int, default=1400)
        parser.add_argument("--cash-for-car", type=int, default=260)
        parser.add_argument("--public-sector", type=int, default=340)
        parser.add_argument("--seed", type=int, default=20260908,
                            help="RNG seed, so the same data is reproducible")

    def handle(self, *args, **options):
        if not settings.SAMPLE_DB:
            raise CommandError(
                "Refusing to run: USE_SAMPLE_DB is not True.\n"
                "This command creates and drops tables. It will not point at "
                "the real loan database."
            )

        config = settings.DATABASES["loans"]
        if config["NAME"] == "dash_loans":
            raise CommandError(
                f"Refusing to run against a database named {config['NAME']!r}."
            )

        random.seed(options["seed"])
        self._ensure_database(config)
        self._create_tables()
        self._populate(options)

    # --- schema ---------------------------------------------------------------

    def _ensure_database(self, config):
        """CREATE DATABASE if it is not there yet, via the `postgres` database."""
        import psycopg

        dsn = {
            "host": config["HOST"] or "127.0.0.1",
            "port": config["PORT"] or "5432",
            "user": config["USER"] or "postgres",
            "dbname": "postgres",
        }
        if config.get("PASSWORD"):
            dsn["password"] = config["PASSWORD"]

        with psycopg.connect(autocommit=True, **dsn) as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (config["NAME"],)
            ).fetchone()
            if exists:
                self.stdout.write(f"Database {config['NAME']} already exists.")
            else:
                conn.execute(f'CREATE DATABASE "{config["NAME"]}"')
                self.stdout.write(self.style.SUCCESS(f"Created database {config['NAME']}."))

    def _create_tables(self):
        connection = connections["loans"]
        existing = set(connection.introspection.table_names())
        with connection.schema_editor() as editor:
            for model in reversed(MODELS):
                if model._meta.db_table in existing:
                    editor.delete_model(model)
            for model in MODELS:
                editor.create_model(model)
        self.stdout.write(f"Created {len(MODELS)} tables from the model definitions.")

    # --- data -----------------------------------------------------------------

    def _populate(self, options):
        connection = connections["loans"]
        admins = self._admins(connection)

        counts = {
            CashbackRequest: options["cashback"],
            CashForCarRequest: options["cash_for_car"],
            PublicSectorRequest: options["public_sector"],
        }
        prefixes = {CashbackRequest: "CB", CashForCarRequest: "CFC",
                    PublicSectorRequest: "PS"}
        # Taken from the models rather than restated, so sample data always
        # uses the same request_type strings production does. When these were
        # duplicated, the seeded database agreed with a wrong assumption and
        # the verification script had nothing to catch.
        request_types = REQUEST_TYPE_BY_MODEL

        total_docs = total_comments = total_history = 0
        for model, count in counts.items():
            rows = [
                self._row(model, prefixes[model], index, admins)
                for index in range(1, count + 1)
            ]
            self._insert(connection, model, rows)
            docs, comments, history = self._children(
                request_types[model], rows, admins
            )
            self._insert(connection, RequestDocument, docs)
            self._insert(connection, LoanComment, comments)
            self._insert(connection, ApprovalAuditLog, history)
            total_docs += len(docs)
            total_comments += len(comments)
            total_history += len(history)
            self.stdout.write(f"  {model.PRODUCT_LABEL}: {count} applications")

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {sum(counts.values())} applications, {total_docs} documents, "
                f"{total_comments} comments, {total_history} history entries."
            )
        )

    def _admins(self, connection):
        rows = []
        now = datetime.now(timezone.utc)
        for index, (name, role) in enumerate(
            [
                ("Ifeoma Balogun", "reviewer"),
                ("Kola Adeniyi", "credit_supervisor"),
                ("Rukayat Sanni", "internal_control"),
                ("Peter Umoh", "operations"),
                ("Grace Etim", "super_admin"),
                ("Damilola Ige", "floauto"),
            ],
            start=1,
        ):
            rows.append(
                {
                    "id": index,
                    "full_name": name,
                    "email": f"{name.split()[0].lower()}@example.invalid",
                    "role": role,
                    "is_active": True,
                    "must_change_password": False,
                    "notifications_read_at": None,
                    "created_at": now - timedelta(days=400),
                    "updated_at": now - timedelta(days=10),
                }
            )
        # No password_hash here: the model deliberately does not map it, so the
        # generated development table has no such column. Nothing in a
        # reporting portal should be able to read it, in dev or in production.
        self._insert(connection, Admin, rows)
        return [row["id"] for row in rows]

    REJECTION_REASONS = {
        "REVIEW": [
            "Incomplete documentation.", "Could not verify identity (BVN mismatch).",
            "Duplicate application.", "Applicant withdrew the request.",
            "Could not verify employment.",
        ],
        "CREDIT": [
            "Affordability below threshold.", "Adverse credit bureau record.",
            "Debt-to-income ratio too high.", "Insufficient bank statement history.",
            "Could not verify employment.",
        ],
        "CONTROL": [
            "Outside product policy.", "Guarantor could not be verified.",
            "Inconsistent information across documents.",
        ],
        "DISBURSEMENT": [
            "Applicant withdrew the request.", "Account details invalid.",
        ],
    }
    CORRECTION_MESSAGES = [
        "Bank statement is illegible - please re-upload.",
        "Upload a valid government-issued ID.",
        "Utility bill older than 3 months.",
        "Employment letter missing signature.",
        "Name on account does not match application.",
        "Please provide the last 6 months of payslips.",
    ]

    def _spine(self, admins, created):
        """A plausible path through the approval pipeline.

        Each stage either passes, sends the application back for correction
        (which may later be resolved and continue), rejects it, or leaves it
        sitting there. Stage hand-offs take hours to days, with the credit
        stage deliberately the slowest so the bottleneck view has something
        to find.
        """
        spine = {
            "reviewed": False, "reviewed_by_id": None, "reviewed_at": None, "review_note": None,
            "credit_approved": False, "credit_approved_by_id": None,
            "credit_approved_at": None, "credit_note": None,
            "control_approved": False, "control_approved_by_id": None,
            "control_approved_at": None, "control_note": None,
            "disbursed": False, "disbursed_by_id": None, "disbursed_at": None,
            "disbursement_note": None,
            "rejected": False, "rejected_by_id": None, "rejected_at": None,
            "rejection_reason": None, "rejected_stage": None,
            "correction_requested": False, "correction_message": None,
            "correction_requested_by_id": None, "correction_requested_at": None,
            "correction_count": 0, "correction_stage": None,
        }
        now = datetime.now(timezone.utc)
        step = created
        # (stage, flag, note field, note, hours range to complete, P(stop here),
        #  P(reject | stop), P(correction | continue))
        stages = [
            ("REVIEW", "reviewed", "review_note", "Documents complete.", (2, 60), 0.16, 0.30, 0.22),
            ("CREDIT", "credit_approved", "credit_note", "Within policy.", (12, 170), 0.22, 0.55, 0.10),
            ("CONTROL", "control_approved", "control_note", "Checks passed.", (4, 70), 0.10, 0.35, 0.06),
            ("DISBURSEMENT", "disbursed", "disbursement_note", "Value given.", (2, 48), 0.06, 0.30, 0.0),
        ]
        for stage, flag, note_field, note, (lo, hi), p_stop, p_reject, p_corr in stages:
            if random.random() < p_corr:
                # Sent back for correction; the applicant may or may not fix it.
                step += timedelta(hours=random.randint(lo, hi))
                if step > now:
                    return spine
                spine["correction_count"] += 1
                spine["correction_requested_by_id"] = random.choice(admins)
                spine["correction_requested_at"] = step
                spine["correction_stage"] = stage
                spine["correction_message"] = random.choice(self.CORRECTION_MESSAGES)
                if random.random() < 0.35:
                    spine["correction_requested"] = True   # still waiting on the applicant
                    return spine
                step += timedelta(hours=random.randint(6, 120))
                if random.random() < 0.15:
                    spine["correction_count"] += 1
                    step += timedelta(hours=random.randint(6, 72))
            step += timedelta(hours=random.randint(lo, hi))
            if step > now:
                return spine
            if random.random() < p_stop:
                if random.random() < p_reject:
                    spine["rejected"] = True
                    spine["rejected_by_id"] = random.choice(admins)
                    spine["rejected_at"] = step
                    spine["rejection_reason"] = random.choice(self.REJECTION_REASONS[stage])
                    spine["rejected_stage"] = stage
                return spine
            spine[flag] = True
            spine[f"{flag}_by_id"] = random.choice(admins)
            spine[f"{flag}_at"] = step
            spine[note_field] = note
        return spine

    def _created_at(self):
        """Weighted towards recent weeks, with quieter weekends."""
        now = datetime.now(timezone.utc)
        while True:
            if random.random() < 0.75:
                days = random.randint(0, 120)
            else:
                days = random.randint(121, 540)
            created = now - timedelta(days=days, hours=random.randint(0, 23),
                                      minutes=random.randint(0, 59))
            if created.weekday() >= 5 and random.random() < 0.6:
                continue
            return created

    def _row(self, model, prefix, index, admins):
        created = self._created_at()
        first = random.choice(FIRST_NAMES)
        last = random.choice(LAST_NAMES)
        amount = random.choice([150, 250, 400, 500, 750, 1000, 1500, 2500, 5000]) * 1000

        row = {
            "id": index,
            "reference_no": f"{prefix}-{index:05d}",
            "title": random.choice(["Mr", "Mrs", "Miss", "Dr", None]),
            "first_name": first,
            "gender": random.choice(["Male", "Female"]),
            "date_of_birth": (datetime.now() - timedelta(days=random.randint(7500, 20000))).date(),
            "marital_status": random.choice(["Single", "Married", "Divorced", None]),
            "bvn": f"{random.randint(10**10, 10**11 - 1)}",
            "nin": f"{random.randint(10**10, 10**11 - 1)}",
            "account_officer": random.choice(OFFICERS),
            "email": f"{first.lower()}.{last.lower()}@example.invalid",
            "phone": f"080{random.randint(10000000, 99999999)}",
            "residential_address": f"{random.randint(1, 240)} {random.choice(LAST_NAMES)} Street",
            "state": random.choice(STATES),
            "lga": f"{random.choice(STATES)} Central",
            "next_of_kin_name": f"{random.choice(FIRST_NAMES)} {last}",
            "next_of_kin_phone": f"081{random.randint(10000000, 99999999)}",
            "next_of_kin_relation": random.choice(["Spouse", "Sibling", "Parent", "Child"]),
            "loan_amount": amount,
            "tenor_months": random.choice([3, 6, 9, 12, 18, 24, 36]),
            "loan_purpose": random.choice(
                ["Working capital", "School fees", "Home improvement",
                 "Medical expenses", "Vehicle purchase", "Business expansion"]
            ),
            "consent_given": True,
            "agreement_accepted": random.random() < 0.85,
            "agreement_accepted_name": f"{first} {last}",
            "agreement_accepted_at": created + timedelta(hours=2),
            "is_legacy": random.random() < 0.12,
            "created_at": created,
            "updated_at": created + timedelta(days=random.randint(0, 30)),
        }
        row.update(self._spine(admins, created))

        if model is CashbackRequest:
            row.update({
                "surname": last,
                "middle_name": random.choice(FIRST_NAMES + [None]),
                "mothers_maiden_name": random.choice(LAST_NAMES),
                "nationality": "Nigerian",
                "employment_status": random.choice(["Employed", "Self-employed", "Retired"]),
                "employer_business": random.choice(["Dangote Group", "Self", "Federal Govt", "MTN"]),
                "occupation": random.choice(OCCUPATIONS),
                "monthly_income": random.randint(80, 900) * 1000,
                "bank_name": random.choice(BANKS),
                "account_no": f"{random.randint(1000000000, 9999999999)}",
                "security_details": random.choice(["Salary domiciliation", "Guarantor", None]),
            })
        elif model is CashForCarRequest:
            make, vmodel = random.choice(VEHICLES)
            valuation = amount * random.uniform(1.4, 2.6)
            row.update({
                "last_name": last,
                "middle_name": random.choice(FIRST_NAMES + [None]),
                "sex": row["gender"],
                "account_number": f"{random.randint(1000000000, 9999999999)}",
                "account_no": f"{random.randint(1000000000, 9999999999)}",
                "bank_name": random.choice(BANKS),
                "occupation": random.choice(OCCUPATIONS),
                "industry": random.choice(["Retail", "Logistics", "Education", "Health"]),
                "proof_of_funds": None, "source_of_repayment": "Business income",
                "vehicle_make": make, "vehicle_model": vmodel,
                "vehicle_year": random.randint(2010, 2023),
                "vehicle_vin": f"VIN{random.randint(10**9, 10**10 - 1)}",
                "vehicle_plate_number": f"ABC-{random.randint(100, 999)}XY",
                "flograde_valuation": round(valuation, 2),
                "security_coverage_ratio": round(valuation / amount, 4),
                "floauto_outlet_location": random.choice(["Lekki", "Ikeja", "Abuja"]),
                "asset_pledged_description": f"{make} {vmodel}",
                "documents_in_custody": "Original vehicle papers",
                "custody_holder": "Dash MFB", "ncr_status": "CLEAR",
                "floauto_sale_guarantee": True,
                "valuation_shortfall_guarantee": random.random() < 0.5,
                "buyout_obligation": random.random() < 0.4,
                "justification": None, "repayment_structure": "Equal monthly instalments",
                "rate_floauto_share": 2.5, "rate_dash_share": 3.5,
                "fee_floauto_share": round(amount * random.uniform(0.008, 0.03), 2),
                "fee_dash_share": round(amount * random.uniform(0.01, 0.035), 2),
                "fee_insurance_share": round(amount * 0.005, 2),
                "bank_reference_statement": None, "credit_bureau_search_ref": None,
                "bank_analysis_status": random.choice(["COMPLETE", "PENDING", None]),
                "bank_analysis_job_id": None, "bank_analysis_id": None,
                "bank_analysis_at": None, "bank_analysis_json": None,
                "avg_monthly_income": random.randint(150, 900) * 1000,
                "avg_balance": random.randint(20, 400) * 1000,
                "salary_earner": random.random() < 0.6,
                "scorecard_json": None,
                "scorecard_verdict": random.choice(["PASS", "REFER", None]),
                "verification_checked_at": None,
                "floauto_evaluated": random.random() < 0.7,
                "floauto_evaluated_by_id": None, "floauto_evaluated_at": None,
                "floauto_recommendation": None,
                "floauto_valuation": round(valuation, 2),
                "floauto_ltv": round(amount / valuation, 4),
                "floauto_recommended_amount": amount,
                "floauto_recommended_tenor": row["tenor_months"],
                "applicant_accepted": random.random() < 0.6,
                "applicant_accepted_at": None,
                "applicant_declined": False, "applicant_declined_at": None,
                "statement_payment_ref": None, "statement_payment_amount": None,
                "statement_payment_method": None, "statement_payment_pages": None,
                "statement_payment_declared_at": None,
                "statement_payment_confirmed": False,
                "statement_payment_confirmed_by_id": None,
                "statement_payment_confirmed_at": None,
            })
        else:
            row.update({
                "last_name": last,
                "employer_mda": random.choice(MDAS),
                "ippis_number": f"{random.randint(100000, 999999)}",
                "grade_level": f"GL{random.randint(6, 16):02d}",
                "date_first_appointed": (datetime.now() - timedelta(days=random.randint(700, 9000))).date(),
                "net_monthly_salary": random.randint(90, 500) * 1000,
                "salary_bank_name": random.choice(BANKS),
                "salary_account_no": f"{random.randint(1000000000, 9999999999)}",
                "repayment_source": "Salary deduction at source",
            })
        return row

    def _children(self, request_type, rows, admins):
        docs, comments, history = [], [], []
        for row in rows:
            request_id = row["id"]
            for doc_type in random.sample(DOC_TYPES, random.randint(1, 4)):
                docs.append({
                    "request_type": request_type,
                    "request_id": request_id,
                    "doc_type": doc_type,
                    "file_url": None,
                    "file_name": f"{doc_type.lower()}.png",
                    "mime_type": "image/png",
                    "data": TINY_PNG,
                    "uploaded_at": row["created_at"] + timedelta(minutes=random.randint(1, 90)),
                })
            if random.random() < 0.3:
                comments.append({
                    "request_type": request_type,
                    "request_id": request_id,
                    "author_id": random.choice(admins),
                    "body": random.choice([
                        "Called the applicant to confirm employment details.",
                        "Recommend approval at a reduced amount.",
                        "Awaiting updated bank statement.",
                    ]),
                    "is_recommendation": random.random() < 0.4,
                    "created_at": row["created_at"] + timedelta(hours=random.randint(1, 72)),
                })
            # History follows the application's actual path, so the journey
            # view's audit trail agrees with its timeline.
            events = [("SUBMITTED", None, "PENDING", row["created_at"], None)]
            previous = "PENDING"
            for flag, code in (("reviewed", "REVIEWED"), ("credit_approved", "CREDIT_APPROVED"),
                               ("control_approved", "CONTROL_APPROVED"), ("disbursed", "DISBURSED")):
                if row[flag]:
                    events.append((code, previous, code, row[f"{flag}_at"], row.get(
                        {"reviewed": "review_note", "credit_approved": "credit_note",
                         "control_approved": "control_note", "disbursed": "disbursement_note"}[flag])))
                    previous = code
            if row["correction_requested_at"]:
                events.append(("CORRECTION_REQUESTED", previous, "CORRECTION_REQUESTED",
                               row["correction_requested_at"], row["correction_message"]))
            if row["rejected"]:
                events.append(("REJECTED", previous, "REJECTED", row["rejected_at"], row["rejection_reason"]))
            for action, frm, to, at, note in events:
                history.append({
                    "request_type": request_type,
                    "request_id": request_id,
                    "actor_id": None if action == "SUBMITTED" else random.choice(admins),
                    "action": action,
                    "from_status": frm,
                    "to_status": to,
                    "note": note,
                    "created_at": at,
                })
        return docs, comments, history

    def _insert(self, connection, model, rows, extra=None):
        """Insert rows with raw SQL.

        Deliberately not the ORM: the router raises on any write routed to a
        loans model, which is exactly the protection this application wants.
        Seeding is the one legitimate exception, and going around the ORM keeps
        that exception from weakening the rule.
        """
        if not rows:
            return
        table = model._meta.db_table
        columns = list(rows[0].keys()) + list((extra or {}).keys())
        placeholders = ", ".join(["%s"] * len(columns))
        quoted = ", ".join(f'"{c}"' for c in columns)
        sql = f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})'
        values = [
            [row[c] for c in rows[0].keys()] + list((extra or {}).values())
            for row in rows
        ]
        with connection.cursor() as cursor:
            cursor.executemany(sql, values)
