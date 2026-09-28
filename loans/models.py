"""Read-only mapping onto the existing loan application tables.

`managed = False` on every model means Django never creates, alters or drops
these tables - they belong to the loan origination system, and this portal only
reads them.

Three loan products share one approval workflow. The shared columns live on
`LoanRequest`; each product adds its own. The products are NOT cleanly
unionable - `cashback_request` calls the family name `surname` where the others
use `last_name`, `public_sector_request` has no `middle_name` at all, and
`cash_for_car_request` carries both `gender` and `sex` - so shared behaviour is
expressed through the abstract base and per-model constants rather than by
pretending the schemas match.
"""

from django.db import models

from .status import derive_status, status_expression, status_label


MONEY = {"max_digits": 20, "decimal_places": 4}


class LoanRequestQuerySet(models.QuerySet):
    def visible_to(self, user):
        """Rows this user may see.

        Every list, detail, export and document lookup goes through here, so
        row visibility has exactly one definition. Today any authenticated
        user sees everything; per-user rules are expected later, and this is
        the single place they will be added.

        Deny by default: an unauthenticated or missing user gets nothing, so a
        half-configured account shows an empty page rather than the whole book.
        """
        if user is None or not user.is_authenticated:
            return self.none()
        return self


class LoanRequestManager(models.Manager.from_queryset(LoanRequestQuerySet)):
    """Annotates the derived status onto every queryset.

    Doing it here rather than per view means filtering, ordering, aggregation
    and export all see the same `status` value, computed by the database from
    the same expression - see loans/status.py.
    """

    def get_queryset(self):
        return super().get_queryset().annotate(status=status_expression())


class LoanRequest(models.Model):
    """Columns and behaviour shared by all three loan products."""

    LAST_NAME_FIELD = "last_name"
    PRODUCT_LABEL = "Loan request"
    PRODUCT_CODE = "loan"

    id = models.BigAutoField(primary_key=True)
    reference_no = models.CharField(max_length=20, unique=True)

    title = models.CharField(max_length=20, blank=True, null=True)
    first_name = models.CharField(max_length=60)
    gender = models.CharField(max_length=20, blank=True, null=True)
    date_of_birth = models.DateField()
    marital_status = models.CharField(max_length=20, blank=True, null=True)

    bvn = models.CharField(max_length=20)
    nin = models.CharField(max_length=20)

    account_officer = models.CharField(max_length=120, blank=True, null=True)
    email = models.CharField(max_length=160, blank=True, null=True)
    phone = models.CharField(max_length=20, blank=True, null=True)

    residential_address = models.TextField(blank=True, null=True)
    state = models.CharField(max_length=60, blank=True, null=True)
    lga = models.CharField(max_length=60, blank=True, null=True)

    next_of_kin_name = models.CharField(max_length=120, blank=True, null=True)
    next_of_kin_phone = models.CharField(max_length=20, blank=True, null=True)
    next_of_kin_relation = models.CharField(max_length=60, blank=True, null=True)

    loan_amount = models.DecimalField(blank=True, null=True, **MONEY)
    tenor_months = models.IntegerField(blank=True, null=True)
    loan_purpose = models.TextField(blank=True, null=True)

    consent_given = models.BooleanField()


    reviewed = models.BooleanField()
    reviewed_by = models.ForeignKey(
        "Admin", models.DO_NOTHING, db_column="reviewed_by_id",
        blank=True, null=True, related_name="+",
    )
    reviewed_at = models.DateTimeField(blank=True, null=True)
    review_note = models.TextField(blank=True, null=True)

    credit_approved = models.BooleanField()
    credit_approved_by = models.ForeignKey(
        "Admin", models.DO_NOTHING, db_column="credit_approved_by_id",
        blank=True, null=True, related_name="+",
    )
    credit_approved_at = models.DateTimeField(blank=True, null=True)
    credit_note = models.TextField(blank=True, null=True)

    control_approved = models.BooleanField()
    control_approved_by = models.ForeignKey(
        "Admin", models.DO_NOTHING, db_column="control_approved_by_id",
        blank=True, null=True, related_name="+",
    )
    control_approved_at = models.DateTimeField(blank=True, null=True)
    control_note = models.TextField(blank=True, null=True)

    disbursed = models.BooleanField()
    disbursed_by = models.ForeignKey(
        "Admin", models.DO_NOTHING, db_column="disbursed_by_id",
        blank=True, null=True, related_name="+",
    )
    disbursed_at = models.DateTimeField(blank=True, null=True)
    disbursement_note = models.TextField(blank=True, null=True)

    rejected = models.BooleanField()
    rejected_by = models.ForeignKey(
        "Admin", models.DO_NOTHING, db_column="rejected_by_id",
        blank=True, null=True, related_name="+",
    )
    rejected_at = models.DateTimeField(blank=True, null=True)
    rejection_reason = models.TextField(blank=True, null=True)
    rejected_stage = models.CharField(max_length=40, blank=True, null=True)

    correction_requested = models.BooleanField()
    correction_message = models.TextField(blank=True, null=True)
    correction_requested_by = models.ForeignKey(
        "Admin", models.DO_NOTHING, db_column="correction_requested_by_id",
        blank=True, null=True, related_name="+",
    )
    correction_requested_at = models.DateTimeField(blank=True, null=True)
    correction_count = models.IntegerField()
    correction_stage = models.CharField(max_length=40, blank=True, null=True)

    agreement_accepted = models.BooleanField()
    agreement_accepted_name = models.CharField(max_length=160, blank=True, null=True)
    agreement_accepted_at = models.DateTimeField(blank=True, null=True)

    is_legacy = models.BooleanField()

    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    objects = LoanRequestManager()

    class Meta:
        abstract = True
        ordering = ["-created_at"]


    @property
    def last_name(self):
        """Family name, whichever column this product stores it in."""
        return getattr(self, self.LAST_NAME_FIELD, None)

    @property
    def full_name(self):
        parts = [self.first_name, getattr(self, "middle_name", None), self.last_name]
        return " ".join(p for p in parts if p)

    @property
    def derived_status(self):
        """Status computed in Python.

        `self.status` is normally present as a database annotation; this is the
        same value derived locally, and the two are proven equal by the tests.
        """
        return derive_status(self)

    @property
    def status_display(self):
        return status_label(getattr(self, "status", None) or self.derived_status)

    @property
    def product_code(self):
        return self.PRODUCT_CODE

    @property
    def product_label(self):
        return self.PRODUCT_LABEL

    def __str__(self):
        return f"{self.reference_no} - {self.full_name}"


class CashbackRequest(LoanRequest):
    LAST_NAME_FIELD = "surname"
    PRODUCT_LABEL = "Cashback"
    PRODUCT_CODE = "cashback"

    surname = models.CharField(max_length=60)
    middle_name = models.CharField(max_length=60, blank=True, null=True)
    mothers_maiden_name = models.CharField(max_length=120, blank=True, null=True)
    nationality = models.CharField(max_length=60, blank=True, null=True)

    employment_status = models.CharField(max_length=60, blank=True, null=True)
    employer_business = models.CharField(max_length=160, blank=True, null=True)
    occupation = models.CharField(max_length=120, blank=True, null=True)
    monthly_income = models.DecimalField(blank=True, null=True, **MONEY)

    bank_name = models.CharField(max_length=120, blank=True, null=True)
    account_no = models.CharField(max_length=10, blank=True, null=True)

    security_details = models.TextField(blank=True, null=True)

    class Meta(LoanRequest.Meta):
        abstract = False
        managed = False
        db_table = "cashback_request"
        verbose_name = "cashback request"
        verbose_name_plural = "cashback requests"


class CashForCarRequest(LoanRequest):
    LAST_NAME_FIELD = "last_name"
    PRODUCT_LABEL = "Cash for Car"
    PRODUCT_CODE = "cash-for-car"

    last_name = models.CharField(max_length=60)
    middle_name = models.CharField(max_length=60, blank=True, null=True)

    sex = models.CharField(max_length=20, blank=True, null=True)

    account_number = models.CharField(max_length=20, blank=True, null=True)
    account_no = models.CharField(max_length=10, blank=True, null=True)
    bank_name = models.CharField(max_length=120, blank=True, null=True)

    occupation = models.CharField(max_length=120, blank=True, null=True)
    industry = models.CharField(max_length=120, blank=True, null=True)
    proof_of_funds = models.CharField(max_length=255, blank=True, null=True)
    source_of_repayment = models.CharField(max_length=160, blank=True, null=True)

    vehicle_make = models.CharField(max_length=80, blank=True, null=True)
    vehicle_model = models.CharField(max_length=80, blank=True, null=True)
    vehicle_year = models.IntegerField(blank=True, null=True)
    vehicle_vin = models.CharField(max_length=40, blank=True, null=True)
    vehicle_plate_number = models.CharField(max_length=20, blank=True, null=True)

    flograde_valuation = models.DecimalField(blank=True, null=True, **MONEY)
    security_coverage_ratio = models.DecimalField(blank=True, null=True, **MONEY)
    floauto_outlet_location = models.CharField(max_length=160, blank=True, null=True)
    asset_pledged_description = models.TextField(blank=True, null=True)
    documents_in_custody = models.TextField(blank=True, null=True)
    custody_holder = models.CharField(max_length=120, blank=True, null=True)
    ncr_status = models.CharField(max_length=60, blank=True, null=True)

    floauto_sale_guarantee = models.BooleanField()
    valuation_shortfall_guarantee = models.BooleanField()
    buyout_obligation = models.BooleanField()

    justification = models.TextField(blank=True, null=True)
    repayment_structure = models.CharField(max_length=255, blank=True, null=True)
    rate_floauto_share = models.DecimalField(blank=True, null=True, **MONEY)
    rate_dash_share = models.DecimalField(blank=True, null=True, **MONEY)
    fee_floauto_share = models.DecimalField(blank=True, null=True, **MONEY)
    fee_dash_share = models.DecimalField(blank=True, null=True, **MONEY)
    fee_insurance_share = models.DecimalField(blank=True, null=True, **MONEY)

    bank_reference_statement = models.CharField(max_length=255, blank=True, null=True)
    credit_bureau_search_ref = models.CharField(max_length=255, blank=True, null=True)

    bank_analysis_status = models.CharField(max_length=20, blank=True, null=True)
    bank_analysis_job_id = models.CharField(max_length=120, blank=True, null=True)
    bank_analysis_id = models.CharField(max_length=120, blank=True, null=True)
    bank_analysis_at = models.DateTimeField(blank=True, null=True)
    bank_analysis_json = models.TextField(blank=True, null=True)
    avg_monthly_income = models.DecimalField(blank=True, null=True, **MONEY)
    avg_balance = models.DecimalField(blank=True, null=True, **MONEY)
    salary_earner = models.BooleanField(blank=True, null=True)
    scorecard_json = models.TextField(blank=True, null=True)
    scorecard_verdict = models.CharField(max_length=20, blank=True, null=True)

    verification_checked_at = models.DateTimeField(blank=True, null=True)

    floauto_evaluated = models.BooleanField()
    floauto_evaluated_by = models.ForeignKey(
        "Admin", models.DO_NOTHING, db_column="floauto_evaluated_by_id",
        blank=True, null=True, related_name="+",
    )
    floauto_evaluated_at = models.DateTimeField(blank=True, null=True)
    floauto_recommendation = models.TextField(blank=True, null=True)
    floauto_valuation = models.DecimalField(blank=True, null=True, **MONEY)
    floauto_ltv = models.DecimalField(blank=True, null=True, **MONEY)
    floauto_recommended_amount = models.DecimalField(blank=True, null=True, **MONEY)
    floauto_recommended_tenor = models.IntegerField(blank=True, null=True)

    applicant_accepted = models.BooleanField()
    applicant_accepted_at = models.DateTimeField(blank=True, null=True)
    applicant_declined = models.BooleanField()
    applicant_declined_at = models.DateTimeField(blank=True, null=True)

    statement_payment_ref = models.CharField(max_length=40, blank=True, null=True)
    statement_payment_amount = models.DecimalField(blank=True, null=True, **MONEY)
    statement_payment_method = models.CharField(max_length=20, blank=True, null=True)
    statement_payment_pages = models.IntegerField(blank=True, null=True)
    statement_payment_declared_at = models.DateTimeField(blank=True, null=True)
    statement_payment_confirmed = models.BooleanField()
    statement_payment_confirmed_by = models.ForeignKey(
        "Admin", models.DO_NOTHING, db_column="statement_payment_confirmed_by_id",
        blank=True, null=True, related_name="+",
    )
    statement_payment_confirmed_at = models.DateTimeField(blank=True, null=True)

    class Meta(LoanRequest.Meta):
        abstract = False
        managed = False
        db_table = "cash_for_car_request"
        verbose_name = "cash for car request"
        verbose_name_plural = "cash for car requests"


class PublicSectorRequest(LoanRequest):
    LAST_NAME_FIELD = "last_name"
    PRODUCT_LABEL = "Public Sector"
    PRODUCT_CODE = "public-sector"

    last_name = models.CharField(max_length=60)

    employer_mda = models.CharField(max_length=160, blank=True, null=True)
    ippis_number = models.CharField(max_length=40, blank=True, null=True)
    grade_level = models.CharField(max_length=40, blank=True, null=True)
    date_first_appointed = models.DateField(blank=True, null=True)
    net_monthly_salary = models.DecimalField(blank=True, null=True, **MONEY)
    salary_bank_name = models.CharField(max_length=120, blank=True, null=True)
    salary_account_no = models.CharField(max_length=10, blank=True, null=True)
    repayment_source = models.CharField(max_length=160, blank=True, null=True)

    class Meta(LoanRequest.Meta):
        abstract = False
        managed = False
        db_table = "public_sector_request"
        verbose_name = "public sector request"
        verbose_name_plural = "public sector requests"


PRODUCTS = {
    model.PRODUCT_CODE: model
    for model in (CashbackRequest, CashForCarRequest, PublicSectorRequest)
}


REQUEST_TYPE_BY_MODEL = {
    CashbackRequest: "cashback",
    CashForCarRequest: "cfc",
    PublicSectorRequest: "psl",
}


class Admin(models.Model):
    """Staff of the loan origination system.

    Referenced by every `*_by_id` column. This portal does not authenticate
    against this table - it has its own Django users - but it reads it to turn
    an actor id into a name.
    """

    id = models.BigAutoField(primary_key=True)
    full_name = models.CharField(max_length=120)
    email = models.CharField(max_length=160)

    ROLE_CHOICES = [
        ("super_admin", "Super admin"),
        ("reviewer", "Reviewer"),
        ("credit_supervisor", "Credit supervisor"),
        ("internal_control", "Internal control"),
        ("operations", "Operations"),
        ("viewer", "Viewer"),
        ("floauto", "FloAuto"),
    ]
    role = models.CharField(max_length=60, choices=ROLE_CHOICES)
    is_active = models.BooleanField()
    must_change_password = models.BooleanField()
    notifications_read_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "admins"
        ordering = ["full_name"]

    def __str__(self):
        return self.full_name


class RequestDocumentManager(models.Manager):
    """Defers the document body on every query.

    `request_document.data` holds the base64-encoded file inline, across 5,000+
    rows. A query that selects it by accident - a bare `.all()` feeding a list
    view - would pull every uploaded ID scan into memory. Deferring by default
    means the expensive column has to be asked for explicitly, via
    `RequestDocument.objects.with_content(...)`.
    """

    def get_queryset(self):
        return super().get_queryset().defer("data")

    def with_content(self, **filters):
        """The one path that loads document bodies. Use for a single file."""
        return super().get_queryset().filter(**filters)


class RequestDocument(models.Model):
    id = models.BigAutoField(primary_key=True)
    request_type = models.CharField(max_length=20)
    request_id = models.BigIntegerField()
    doc_type = models.CharField(max_length=60)
    file_url = models.TextField(blank=True, null=True)
    file_name = models.CharField(max_length=200, blank=True, null=True)
    mime_type = models.CharField(max_length=100)
    data = models.TextField()
    uploaded_at = models.DateTimeField()

    objects = RequestDocumentManager()

    class Meta:
        managed = False
        db_table = "request_document"
        ordering = ["doc_type", "uploaded_at"]

    def __str__(self):
        return self.file_name or f"{self.doc_type} #{self.pk}"


class ApprovalAuditLog(models.Model):
    id = models.BigAutoField(primary_key=True)
    request_type = models.CharField(max_length=20)
    request_id = models.BigIntegerField()
    actor = models.ForeignKey(
        Admin, models.DO_NOTHING, db_column="actor_id",
        blank=True, null=True, related_name="+",
    )
    action = models.CharField(max_length=40)
    from_status = models.CharField(max_length=40, blank=True, null=True)
    to_status = models.CharField(max_length=40, blank=True, null=True)
    note = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "approval_audit_log"
        ordering = ["-created_at"]


class LoanComment(models.Model):
    id = models.BigAutoField(primary_key=True)
    request_type = models.CharField(max_length=20)
    request_id = models.BigIntegerField()
    author = models.ForeignKey(
        Admin, models.DO_NOTHING, db_column="author_id",
        blank=True, null=True, related_name="+",
    )
    body = models.TextField()
    is_recommendation = models.BooleanField()
    created_at = models.DateTimeField()

    class Meta:
        managed = False
        db_table = "loan_comment"
        ordering = ["-created_at"]
