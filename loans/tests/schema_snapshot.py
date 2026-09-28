"""The real `dash_loans` schema, as read from information_schema on 2026-09-08.

Transcribed from the production database so the models can be checked against
it without a network route. `seed_sample_db` builds the development database
FROM the models, so it can never catch a model that has drifted from the real
table - this snapshot is what closes that gap offline. `scripts/verify_schema.py`
does the same check live, and is the authority; this is the version that runs in
CI and on a laptop with no tunnel.

Refresh it after any upstream schema change:

    SELECT table_name, column_name FROM information_schema.columns
    WHERE table_schema = 'public' ORDER BY table_name, ordinal_position;
"""

MAPPED_TABLES = {
    "cashback_request": [
        "id", "reference_no", "title", "first_name", "surname", "middle_name",
        "email", "phone", "bvn", "nin", "gender", "marital_status",
        "mothers_maiden_name", "date_of_birth", "nationality",
        "residential_address", "state", "lga", "employment_status",
        "employer_business", "occupation", "monthly_income", "bank_name",
        "account_no", "loan_amount", "tenor_months", "loan_purpose",
        "security_details", "next_of_kin_name", "next_of_kin_phone",
        "next_of_kin_relation", "consent_given", "reviewed", "reviewed_by_id",
        "reviewed_at", "review_note", "credit_approved", "credit_approved_by_id",
        "credit_approved_at", "credit_note", "control_approved",
        "control_approved_by_id", "control_approved_at", "control_note",
        "disbursed", "disbursed_by_id", "disbursed_at", "disbursement_note",
        "rejected", "rejected_by_id", "rejected_at", "rejection_reason",
        "rejected_stage", "created_at", "updated_at", "agreement_accepted",
        "agreement_accepted_name", "agreement_accepted_at", "is_legacy",
        "account_officer", "correction_count", "correction_message",
        "correction_requested", "correction_requested_at",
        "correction_requested_by_id", "correction_stage",
    ],
    "cash_for_car_request": [
        "id", "reference_no", "title", "first_name", "middle_name", "last_name",
        "gender", "sex", "date_of_birth", "marital_status", "bvn", "nin",
        "account_officer", "account_number", "email", "phone",
        "residential_address", "state", "lga", "occupation", "industry",
        "proof_of_funds", "source_of_repayment", "bank_name", "account_no",
        "next_of_kin_name", "next_of_kin_phone", "next_of_kin_relation",
        "vehicle_make", "vehicle_model", "vehicle_year", "vehicle_vin",
        "flograde_valuation", "security_coverage_ratio",
        "floauto_outlet_location", "asset_pledged_description",
        "documents_in_custody", "custody_holder", "ncr_status",
        "floauto_sale_guarantee", "valuation_shortfall_guarantee",
        "buyout_obligation", "loan_amount", "tenor_months", "loan_purpose",
        "justification", "repayment_structure", "rate_floauto_share",
        "rate_dash_share", "fee_floauto_share", "fee_dash_share",
        "fee_insurance_share", "bank_reference_statement",
        "credit_bureau_search_ref", "consent_given", "bank_analysis_status",
        "bank_analysis_job_id", "verification_checked_at", "is_legacy",
        "agreement_accepted", "agreement_accepted_name", "agreement_accepted_at",
        "floauto_evaluated", "floauto_evaluated_by_id", "floauto_evaluated_at",
        "floauto_recommendation", "floauto_valuation", "floauto_ltv",
        "floauto_recommended_amount", "floauto_recommended_tenor",
        "applicant_accepted", "applicant_accepted_at", "applicant_declined",
        "applicant_declined_at", "reviewed", "reviewed_by_id", "reviewed_at",
        "review_note", "credit_approved", "credit_approved_by_id",
        "credit_approved_at", "credit_note", "control_approved",
        "control_approved_by_id", "control_approved_at", "control_note",
        "disbursed", "disbursed_by_id", "disbursed_at", "disbursement_note",
        "rejected", "rejected_by_id", "rejected_at", "rejection_reason",
        "rejected_stage", "correction_requested", "correction_message",
        "correction_requested_by_id", "correction_requested_at",
        "correction_count", "correction_stage", "created_at", "updated_at",
        "vehicle_plate_number", "bank_analysis_id", "bank_analysis_at",
        "avg_monthly_income", "avg_balance", "salary_earner",
        "bank_analysis_json", "scorecard_json", "scorecard_verdict",
        "statement_payment_ref", "statement_payment_amount",
        "statement_payment_method", "statement_payment_pages",
        "statement_payment_declared_at", "statement_payment_confirmed",
        "statement_payment_confirmed_by_id", "statement_payment_confirmed_at",
    ],
    "public_sector_request": [
        "id", "reference_no", "title", "first_name", "last_name", "gender",
        "date_of_birth", "marital_status", "bvn", "nin", "account_officer",
        "email", "phone", "residential_address", "state", "lga", "employer_mda",
        "ippis_number", "grade_level", "date_first_appointed",
        "net_monthly_salary", "salary_bank_name", "salary_account_no",
        "loan_amount", "tenor_months", "loan_purpose", "repayment_source",
        "next_of_kin_name", "next_of_kin_phone", "next_of_kin_relation",
        "consent_given", "reviewed", "reviewed_by_id", "reviewed_at",
        "review_note", "credit_approved", "credit_approved_by_id",
        "credit_approved_at", "credit_note", "control_approved",
        "control_approved_by_id", "control_approved_at", "control_note",
        "disbursed", "disbursed_by_id", "disbursed_at", "disbursement_note",
        "rejected", "rejected_by_id", "rejected_at", "rejection_reason",
        "rejected_stage", "created_at", "updated_at", "agreement_accepted",
        "agreement_accepted_name", "agreement_accepted_at", "is_legacy",
        "correction_count", "correction_message", "correction_requested",
        "correction_requested_at", "correction_requested_by_id",
        "correction_stage",
    ],
    "admins": [
        "id", "full_name", "email", "password_hash", "role", "is_active",
        "must_change_password", "notifications_read_at", "created_at",
        "updated_at", "roles",
    ],
    "request_document": [
        "id", "request_type", "request_id", "doc_type", "file_url",
        "uploaded_at", "file_name", "mime_type", "data",
    ],
    "approval_audit_log": [
        "id", "request_type", "request_id", "actor_id", "action", "from_status",
        "to_status", "note", "created_at",
    ],
    "loan_comment": [
        "id", "request_type", "request_id", "author_id", "body",
        "is_recommendation", "created_at",
    ],
}

INTENTIONALLY_UNMAPPED = {
    "admins": {
        "password_hash": "a reporting portal must never be able to read password hashes",
        "roles": "authorisation is not delegated to the loan system's role array",
    },
}

UNMAPPED_TABLES = {
    "tenor_extension": "a fourth request type - the live request_document table already carries one 'extension' row, so this is in use; see README",
    "payroll_record": "public-sector payroll reference data, not an application",
    "legacy_approval": "pre-migration approval records, superseded by approval_audit_log",
    "legacy_note": "pre-migration comments, superseded by loan_comment",
    "legacy_nok": "pre-migration next-of-kin records, now inline on the request",
    "lga": "state/LGA lookup list",
    "setting": "runtime configuration for the origination system",
    "referral_code": "marketing referral codes",
    "floauto_outlet": "FloAuto dealership list",
    "idempotency_key": "request de-duplication for the origination API",
    "phone_otp": "one-time passcodes - transient auth state, never reporting data",
}
