"""Fixed reference data for the synthetic bank. Names and cities are placeholders."""

from __future__ import annotations

# (branch_code, branch_name, city, region, share of volume)
BRANCHES: list[tuple[str, str, str, str, float]] = [
    ("001", "Salmiya", "Salmiya", "CENTRAL", 0.16),
    ("002", "Kuwait City Main", "Kuwait City", "CENTRAL", 0.14),
    ("003", "Hawalli", "Hawalli", "CENTRAL", 0.08),
    ("004", "Sharq", "Kuwait City", "CENTRAL", 0.07),
    ("005", "Jahra", "Jahra", "NORTH", 0.08),
    ("006", "Sulaibikhat", "Sulaibikhat", "NORTH", 0.06),
    ("007", "Doha", "Doha", "NORTH", 0.05),
    ("008", "Abdali", "Abdali", "NORTH", 0.04),
    ("009", "Fahaheel", "Fahaheel", "SOUTH", 0.09),
    ("010", "Ahmadi", "Ahmadi", "SOUTH", 0.08),
    ("011", "Mangaf", "Mangaf", "SOUTH", 0.08),
    ("012", "Wafra", "Wafra", "SOUTH", 0.07),
]
ALL_BRANCHES = [b[0] for b in BRANCHES]
NORTH_BRANCHES = [b[0] for b in BRANCHES if b[3] == "NORTH"]

ACCOUNT_CLASSES: list[tuple[str, str, str]] = [
    ("SAVREG", "Regular Savings", "CASA"),
    ("SAVPLS", "Savings Plus", "CASA"),
    ("CURIND", "Current Account - Individual", "CASA"),
    ("CURCOR", "Current Account - Corporate", "CASA"),
    ("SALACC", "Salary Account", "CASA"),
    ("TD03M", "Term Deposit 3 Months", "TD"),
    ("TD06M", "Term Deposit 6 Months", "TD"),
    ("TD12M", "Term Deposit 12 Months", "TD"),
]
FAMILY = {c[0]: c[2] for c in ACCOUNT_CLASSES}

# (txn_code, description, txn_group, drcr_ind)
TXN_TYPES: list[tuple[str, str, str, str]] = [
    ("TRF1", "Own account transfer", "TRANSFER", "D"),
    ("TRF2", "Transfer to beneficiary", "TRANSFER", "D"),
    ("TRF3", "Incoming transfer", "TRANSFER", "C"),
    ("TRF4", "International transfer", "TRANSFER", "D"),
    ("SAL1", "Salary credit", "TRANSFER", "C"),
    ("BIL1", "Utility bill payment", "BILLPAY", "D"),
    ("BIL2", "Telecom bill payment", "BILLPAY", "D"),
    ("BIL3", "Credit card bill payment", "BILLPAY", "D"),
    ("CSH1", "Cash withdrawal", "CASH", "D"),
    ("CSH2", "Cash deposit", "CASH", "C"),
    ("CRD1", "Card purchase", "CARD", "D"),
    ("CRD2", "Card refund", "CARD", "C"),
    ("CHG1", "Account maintenance charge", "CHARGE", "D"),
    ("CHG2", "Transfer fee", "CHARGE", "D"),
]
TXN_DRCR = {t[0]: t[3] for t in TXN_TYPES}
TXN_GROUP = {t[0]: t[2] for t in TXN_TYPES}

# (wf_code, wf_name, app_name, current version)
WF_DEFINITIONS: list[tuple[str, str, str, int]] = [
    ("LOAN_ORIG", "Personal Loan Origination", "Lending", 2),
    ("CASA_ONBOARD", "CASA Account Opening", "Origination", 1),
]

# (wf_code, stage_code, stage_seq, stage_name, stage_type, sla_hours, assigned_role)
WF_STAGES: list[tuple[str, str, int, str, str, int | None, str | None]] = [
    ("LOAN_ORIG", "DRAFT", 10, "Draft application", "START", None, "Customer / RM"),
    ("LOAN_ORIG", "KYC_CHECK", 20, "KYC check", "SYSTEM", 4, "System"),
    ("LOAN_ORIG", "DOC_VERIFY", 30, "Document verification", "TASK", 24, "Ops maker"),
    ("LOAN_ORIG", "CREDIT_ASSESS", 40, "Credit assessment", "TASK", 48, "Credit analyst"),
    ("LOAN_ORIG", "APPROVAL", 50, "Credit approval", "APPROVAL", 24, "Credit approver"),
    ("LOAN_ORIG", "OFFER_ACCEPT", 60, "Offer acceptance", "TASK", 72, "Customer"),
    ("LOAN_ORIG", "DISBURSE", 70, "Disbursement", "SYSTEM", 8, "System (core)"),
    ("LOAN_ORIG", "CLOSED", 80, "Closed", "END", None, None),
    ("LOAN_ORIG", "REJECTED", 90, "Rejected", "END", None, None),
    ("LOAN_ORIG", "CANCELLED", 99, "Cancelled", "END", None, None),
    ("CASA_ONBOARD", "DRAFT", 10, "Draft application", "START", None, "Customer"),
    ("CASA_ONBOARD", "EKYC", 20, "eKYC", "SYSTEM", 1, "System"),
    ("CASA_ONBOARD", "AML_SCREEN", 30, "AML screening", "SYSTEM", 2, "System"),
    ("CASA_ONBOARD", "CHECKER_APPROVAL", 40, "Checker approval", "APPROVAL", 8, "Ops checker"),
    ("CASA_ONBOARD", "ACCOUNT_CREATE", 50, "Account creation", "SYSTEM", 1, "System (core)"),
    ("CASA_ONBOARD", "CLOSED", 60, "Closed", "END", None, None),
    ("CASA_ONBOARD", "REJECTED", 90, "Rejected", "END", None, None),
    ("CASA_ONBOARD", "CANCELLED", 99, "Cancelled", "END", None, None),
]
STAGE_INFO = {(s[0], s[1]): s for s in WF_STAGES}

# Who acts on a stage in the history table (actor_role values)
STAGE_ACTOR_ROLE = {
    "DRAFT": "CUSTOMER",
    "KYC_CHECK": "SYSTEM",
    "DOC_VERIFY": "OPS_MAKER",
    "CREDIT_ASSESS": "CREDIT_ANALYST",
    "APPROVAL": "CREDIT_APPROVER",
    "OFFER_ACCEPT": "CUSTOMER",
    "DISBURSE": "SYSTEM",
    "EKYC": "SYSTEM",
    "AML_SCREEN": "SYSTEM",
    "CHECKER_APPROVAL": "OPS_CHECKER",
    "ACCOUNT_CREATE": "SYSTEM",
}

# Probability that an exited visit breaches its SLA (CREDIT_ASSESS highest, per spec)
SLA_BREACH_P = {
    "KYC_CHECK": 0.06,
    "DOC_VERIFY": 0.14,
    "CREDIT_ASSESS": 0.30,
    "APPROVAL": 0.13,
    "OFFER_ACCEPT": 0.10,
    "DISBURSE": 0.05,
    "EKYC": 0.05,
    "AML_SCREEN": 0.10,
    "CHECKER_APPROVAL": 0.20,
    "ACCOUNT_CREATE": 0.04,
}

LOAN_MAIN = [
    "DRAFT",
    "KYC_CHECK",
    "DOC_VERIFY",
    "CREDIT_ASSESS",
    "APPROVAL",
    "OFFER_ACCEPT",
    "DISBURSE",
]
CASA_MAIN = ["DRAFT", "EKYC", "AML_SCREEN", "CHECKER_APPROVAL", "ACCOUNT_CREATE"]

# (username, display_name, role, branches)
STAFF_USERS: list[tuple[str, str, str, list[str]]] = [
    ("bm_salmiya", "Salmiya Branch Manager", "branch_manager", ["001"]),
    ("rh_north", "Regional Head - North", "regional_head", NORTH_BRANCHES),
    ("ho_analyst", "Head Office MIS Analyst", "head_office", ALL_BRANCHES),
    ("it_ops", "IT Operations Support", "it_support", ALL_BRANCHES),
]
