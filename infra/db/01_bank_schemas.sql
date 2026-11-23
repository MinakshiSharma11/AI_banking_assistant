-- Mock bank systems: core (FLEXCUBE-style), obdx (digital channel), atum (Atumverse).
-- Recreated on every `make seed` so seeding is idempotent. All data is synthetic.

DROP SCHEMA IF EXISTS atum CASCADE;
DROP SCHEMA IF EXISTS obdx CASCADE;
DROP SCHEMA IF EXISTS core CASCADE;
CREATE SCHEMA core;
CREATE SCHEMA obdx;
CREATE SCHEMA atum;

-- core: system of record ---------------------------------------------------
CREATE TABLE core.branch_master (
  branch_code   CHAR(3) PRIMARY KEY,                 -- '001'..'012'
  branch_name   VARCHAR(60) NOT NULL,
  city          VARCHAR(40) NOT NULL,
  region        VARCHAR(20) NOT NULL                 -- 'NORTH','SOUTH','CENTRAL'
);
CREATE TABLE core.customer (
  customer_no   VARCHAR(9) PRIMARY KEY,
  full_name     VARCHAR(100) NOT NULL,               -- PII
  national_id   VARCHAR(12) NOT NULL,                -- PII
  mobile_no     VARCHAR(15),                         -- PII
  segment       CHAR(1) NOT NULL,                    -- 'R' retail,'P' premier,'C' corporate
  home_branch   CHAR(3) REFERENCES core.branch_master,
  created_date  DATE NOT NULL
);
CREATE TABLE core.account_class (
  account_class  VARCHAR(6) PRIMARY KEY,             -- 'SAVREG','CURIND','TD12M'...
  description    VARCHAR(60) NOT NULL,
  product_family VARCHAR(4) NOT NULL                 -- 'CASA','TD'
);
CREATE TABLE core.cust_account (
  account_no        VARCHAR(14) PRIMARY KEY,
  customer_no       VARCHAR(9) REFERENCES core.customer,
  branch_code       CHAR(3) REFERENCES core.branch_master,
  account_class     VARCHAR(6) REFERENCES core.account_class,
  ccy               CHAR(3) NOT NULL DEFAULT 'KWD',
  account_open_date DATE NOT NULL,                   -- business date
  record_stat       CHAR(1) NOT NULL,                -- 'O' open,'C' closed
  auth_stat         CHAR(1) NOT NULL,                -- 'A' authorised,'U' unauthorised
  closed_date       DATE
);
CREATE TABLE core.txn_type (
  txn_code    VARCHAR(4) PRIMARY KEY,
  description VARCHAR(60) NOT NULL,
  txn_group   VARCHAR(10) NOT NULL                   -- 'TRANSFER','BILLPAY','CASH','CARD','CHARGE'
);
CREATE TABLE core.transaction (
  txn_ref     VARCHAR(16) PRIMARY KEY,
  account_no  VARCHAR(14) REFERENCES core.cust_account,
  txn_code    VARCHAR(4) REFERENCES core.txn_type,
  drcr_ind    CHAR(1) NOT NULL,                      -- 'D' debit,'C' credit
  lcy_amount  NUMERIC(18,3) NOT NULL,                -- KWD, 3 decimals
  txn_date    DATE NOT NULL,                         -- business date
  txn_ts      TIMESTAMPTZ NOT NULL,
  channel     VARCHAR(8) NOT NULL,                   -- 'BRANCH','OBDX_MOB','OBDX_WEB','ATM','POS'
  status      CHAR(1) NOT NULL,                      -- 'P' posted,'F' failed,'R' reversed
  session_id  UUID                                   -- set for OBDX channels
);

-- obdx: digital channel ------------------------------------------------------
CREATE TABLE obdx.digx_user (
  user_id         VARCHAR(20) PRIMARY KEY,
  customer_no     VARCHAR(9) REFERENCES core.customer,
  user_type       VARCHAR(9) NOT NULL,               -- 'RETAIL','CORPORATE'
  status          CHAR(1) NOT NULL,                  -- 'A' active,'L' locked,'D' disabled
  registered_at   TIMESTAMPTZ NOT NULL,
  last_login_at   TIMESTAMPTZ,
  failed_attempts SMALLINT NOT NULL DEFAULT 0
);
CREATE TABLE obdx.digx_user_session (
  session_id       UUID PRIMARY KEY,
  user_id          VARCHAR(20) REFERENCES obdx.digx_user,
  channel          VARCHAR(6) NOT NULL,              -- 'MOBILE','WEB'
  device_os        VARCHAR(10),                      -- 'IOS','ANDROID','WINDOWS','MACOS'
  login_at         TIMESTAMPTZ NOT NULL,
  last_activity_at TIMESTAMPTZ NOT NULL,
  logout_at        TIMESTAMPTZ,                      -- NULL = not logged out
  login_status     CHAR(1) NOT NULL,                 -- 'S' success,'F' failed
  failure_reason   VARCHAR(20)                       -- 'BAD_PASSWORD','OTP_FAIL','LOCKED'
);
CREATE TABLE obdx.origination_application (
  application_id VARCHAR(12) PRIMARY KEY,
  customer_no    VARCHAR(9) REFERENCES core.customer,
  account_no     VARCHAR(14) REFERENCES core.cust_account,  -- set when approved
  account_class  VARCHAR(6) REFERENCES core.account_class,
  channel        VARCHAR(6) NOT NULL,                -- 'MOBILE','WEB'
  status         VARCHAR(9) NOT NULL,                -- 'STARTED','SUBMITTED','APPROVED','REJECTED','ABANDONED'
  started_at     TIMESTAMPTZ NOT NULL,
  completed_at   TIMESTAMPTZ
);

-- atum: Atumverse workflow engine --------------------------------------------
CREATE TABLE atum.wf_definition (
  wf_code    VARCHAR(20) PRIMARY KEY,                -- 'LOAN_ORIG','CASA_ONBOARD'
  wf_name    VARCHAR(60) NOT NULL,
  app_name   VARCHAR(20) NOT NULL,                   -- 'Origination','Lending','Serve'
  version    SMALLINT NOT NULL,
  is_active  BOOLEAN NOT NULL DEFAULT true
);
CREATE TABLE atum.wf_stage (
  wf_code       VARCHAR(20) REFERENCES atum.wf_definition,
  stage_code    VARCHAR(20),
  stage_seq     SMALLINT NOT NULL,                   -- display and funnel order
  stage_name    VARCHAR(60) NOT NULL,
  stage_type    VARCHAR(8) NOT NULL,                 -- 'START','TASK','APPROVAL','SYSTEM','END'
  sla_hours     SMALLINT,                            -- NULL for START/END
  assigned_role VARCHAR(30),
  PRIMARY KEY (wf_code, stage_code)
);
CREATE TABLE atum.wf_instance (
  instance_id    VARCHAR(16) PRIMARY KEY,
  wf_code        VARCHAR(20) REFERENCES atum.wf_definition,
  wf_version     SMALLINT NOT NULL,                  -- payload shape differs by version
  reference_no   VARCHAR(20) NOT NULL,               -- application number shown to staff
  customer_no    VARCHAR(9) REFERENCES core.customer,
  branch_code    CHAR(3) REFERENCES core.branch_master,
  current_stage  VARCHAR(20) NOT NULL,
  status         VARCHAR(11) NOT NULL,               -- 'IN_PROGRESS','COMPLETED','REJECTED','CANCELLED'
  created_at     TIMESTAMPTZ NOT NULL,
  updated_at     TIMESTAMPTZ NOT NULL,
  completed_at   TIMESTAMPTZ,
  payload_format VARCHAR(9) NOT NULL,                -- 'JSON','BLOB_GZIP','RAW_TEXT'
  payload_json   JSONB,                              -- set when format = 'JSON'
  payload_blob   BYTEA,                              -- gzip(JSON) when format = 'BLOB_GZIP'
  payload_raw    TEXT,                               -- serialised JSON string when 'RAW_TEXT'
  CHECK (num_nonnulls(payload_json, payload_blob, payload_raw) = 1)
);
CREATE TABLE atum.wf_stage_history (
  history_id   BIGSERIAL PRIMARY KEY,
  instance_id  VARCHAR(16) REFERENCES atum.wf_instance,
  stage_code   VARCHAR(20) NOT NULL,
  entered_at   TIMESTAMPTZ NOT NULL,
  exited_at    TIMESTAMPTZ,                          -- NULL = still in this stage
  action       VARCHAR(8),                           -- 'SUBMIT','APPROVE','REJECT','RETURN','AUTO','CANCEL'
  actor_user   VARCHAR(30),
  actor_role   VARCHAR(30),
  sla_breached BOOLEAN,                              -- set on exit
  event_data   JSONB                                 -- e.g. {"returnReason":"MISSING_DOC"}
);
CREATE TABLE atum.wf_document (
  doc_id      VARCHAR(16) PRIMARY KEY,
  instance_id VARCHAR(16) REFERENCES atum.wf_instance,
  doc_type    VARCHAR(15) NOT NULL,                  -- 'CIVIL_ID','SALARY_CERT','BANK_STMT'
  mime_type   VARCHAR(30) NOT NULL,
  content     BYTEA NOT NULL,                        -- the file (BLOB): never queryable
  size_bytes  INT NOT NULL,
  uploaded_at TIMESTAMPTZ NOT NULL,
  ocr_json    JSONB                                  -- extracted fields, if OCR ran
);
-- Written by the decoder (phase 2): the only Atumverse business table the agent can query.
CREATE TABLE atum.wf_instance_flat (
  instance_id       VARCHAR(16) PRIMARY KEY REFERENCES atum.wf_instance,
  product           VARCHAR(10),
  purpose           VARCHAR(12),
  amount_requested  NUMERIC(18,3),
  tenor_months      SMALLINT,
  salary_band       VARCHAR(9),                      -- '<1000','1000-2499','2500+' (no raw salary)
  employment_type   VARCHAR(12),
  credit_score_band VARCHAR(7),
  dbr_pct           NUMERIC(5,2),
  decision          VARCHAR(8),
  amount_approved   NUMERIC(18,3),
  decoded_at        TIMESTAMPTZ NOT NULL
);
CREATE TABLE atum.decode_error (
  instance_id VARCHAR(16), payload_format VARCHAR(9), error TEXT, seen_at TIMESTAMPTZ
);
