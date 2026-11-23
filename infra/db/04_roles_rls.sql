-- agent_ro: the only role the data agent connects as. Read-only, column-restricted,
-- branch-filtered by row-level security. Applied after seeding, so it must be idempotent.
-- {{AGENT_RO_PASSWORD}} is substituted (as a quoted literal) by mocks/seed/run.py.

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_ro') THEN
    CREATE ROLE agent_ro LOGIN;
  END IF;
END $$;
ALTER ROLE agent_ro WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
  PASSWORD {{AGENT_RO_PASSWORD}};
ALTER ROLE agent_ro SET statement_timeout = '10s';
ALTER ROLE agent_ro SET default_transaction_read_only = on;
ALTER ROLE agent_ro SET search_path = '';

-- Nothing by default, then explicit grants only.
REVOKE ALL ON SCHEMA public FROM agent_ro;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA app, audit FROM agent_ro;
REVOKE ALL ON SCHEMA app, audit FROM agent_ro;

-- Entitlement helper read by every RLS policy. The gateway sets it per request:
--   SET LOCAL app.user_branches = '001,005';   (unset or empty = no rows)
CREATE SCHEMA IF NOT EXISTS sec;
CREATE OR REPLACE FUNCTION sec.user_branches() RETURNS text[]
  LANGUAGE sql STABLE AS $$
  SELECT COALESCE(
    string_to_array(NULLIF(current_setting('app.user_branches', true), ''), ','),
    ARRAY[]::text[])
$$;
REVOKE ALL ON FUNCTION sec.user_branches() FROM PUBLIC;
GRANT USAGE ON SCHEMA sec TO agent_ro;
GRANT EXECUTE ON FUNCTION sec.user_branches() TO agent_ro;

GRANT USAGE ON SCHEMA core, obdx, atum TO agent_ro;

-- core: everything except the PII columns of customer
GRANT SELECT ON core.branch_master, core.account_class, core.txn_type,
                core.cust_account, core.transaction TO agent_ro;
GRANT SELECT (customer_no, segment, home_branch, created_date) ON core.customer TO agent_ro;

-- obdx: no PII columns in these tables
GRANT SELECT ON obdx.digx_user, obdx.digx_user_session, obdx.origination_application TO agent_ro;

-- atum: workflow columns only; payloads, document BLOBs and OCR are never readable
GRANT SELECT ON atum.wf_definition, atum.wf_stage, atum.wf_stage_history,
                atum.wf_instance_flat TO agent_ro;
GRANT SELECT (instance_id, wf_code, wf_version, reference_no, customer_no, branch_code,
              current_stage, status, created_at, updated_at, completed_at, payload_format)
  ON atum.wf_instance TO agent_ro;
GRANT SELECT (doc_id, instance_id, doc_type, mime_type, size_bytes, uploaded_at)
  ON atum.wf_document TO agent_ro;
-- atum.decode_error: no grant (operational table, read via /health only)

-- Row-level security ---------------------------------------------------------
-- Reference tables (branch_master, account_class, txn_type, wf_definition, wf_stage)
-- carry no branch data and stay unfiltered.
ALTER TABLE core.customer                ENABLE ROW LEVEL SECURITY;
ALTER TABLE core.cust_account            ENABLE ROW LEVEL SECURITY;
ALTER TABLE core.transaction             ENABLE ROW LEVEL SECURITY;
ALTER TABLE obdx.digx_user               ENABLE ROW LEVEL SECURITY;
ALTER TABLE obdx.digx_user_session       ENABLE ROW LEVEL SECURITY;
ALTER TABLE obdx.origination_application ENABLE ROW LEVEL SECURITY;
ALTER TABLE atum.wf_instance             ENABLE ROW LEVEL SECURITY;
ALTER TABLE atum.wf_stage_history        ENABLE ROW LEVEL SECURITY;
ALTER TABLE atum.wf_document             ENABLE ROW LEVEL SECURITY;
ALTER TABLE atum.wf_instance_flat        ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS branch_rls ON core.customer;
CREATE POLICY branch_rls ON core.customer FOR SELECT TO agent_ro
  USING (home_branch::text = ANY (sec.user_branches()));

DROP POLICY IF EXISTS branch_rls ON core.cust_account;
CREATE POLICY branch_rls ON core.cust_account FOR SELECT TO agent_ro
  USING (branch_code::text = ANY (sec.user_branches()));

-- Policies that look up another table use `key IN (SELECT ...)`, not a correlated
-- EXISTS: Postgres builds the allowed key set once and hashes it, instead of running
-- the lookup per row (E28: 9 s -> 1.4 s). Same rows are visible either way.
DROP POLICY IF EXISTS branch_rls ON core.transaction;
CREATE POLICY branch_rls ON core.transaction FOR SELECT TO agent_ro
  USING (account_no IN (SELECT a.account_no FROM core.cust_account a
    WHERE a.branch_code::text = ANY (sec.user_branches())));

-- Digital users and applications belong to the customer's home branch.
DROP POLICY IF EXISTS branch_rls ON obdx.digx_user;
CREATE POLICY branch_rls ON obdx.digx_user FOR SELECT TO agent_ro
  USING (customer_no IN (SELECT c.customer_no FROM core.customer c
    WHERE c.home_branch::text = ANY (sec.user_branches())));

DROP POLICY IF EXISTS branch_rls ON obdx.digx_user_session;
CREATE POLICY branch_rls ON obdx.digx_user_session FOR SELECT TO agent_ro
  USING (user_id IN (SELECT u.user_id FROM obdx.digx_user u JOIN core.customer c USING (customer_no)
    WHERE c.home_branch::text = ANY (sec.user_branches())));

DROP POLICY IF EXISTS branch_rls ON obdx.origination_application;
CREATE POLICY branch_rls ON obdx.origination_application FOR SELECT TO agent_ro
  USING (customer_no IN (SELECT c.customer_no FROM core.customer c
    WHERE c.home_branch::text = ANY (sec.user_branches())));

DROP POLICY IF EXISTS branch_rls ON atum.wf_instance;
CREATE POLICY branch_rls ON atum.wf_instance FOR SELECT TO agent_ro
  USING (branch_code::text = ANY (sec.user_branches()));

DROP POLICY IF EXISTS branch_rls ON atum.wf_stage_history;
CREATE POLICY branch_rls ON atum.wf_stage_history FOR SELECT TO agent_ro
  USING (instance_id IN (SELECT i.instance_id FROM atum.wf_instance i
    WHERE i.branch_code::text = ANY (sec.user_branches())));

DROP POLICY IF EXISTS branch_rls ON atum.wf_document;
CREATE POLICY branch_rls ON atum.wf_document FOR SELECT TO agent_ro
  USING (instance_id IN (SELECT i.instance_id FROM atum.wf_instance i
    WHERE i.branch_code::text = ANY (sec.user_branches())));

DROP POLICY IF EXISTS branch_rls ON atum.wf_instance_flat;
CREATE POLICY branch_rls ON atum.wf_instance_flat FOR SELECT TO agent_ro
  USING (instance_id IN (SELECT i.instance_id FROM atum.wf_instance i
    WHERE i.branch_code::text = ANY (sec.user_branches())));

-- gateway_rw: the gateway/agent process identity for platform tables. It may read staff
-- users (mock SSO) and append audit rows. It cannot read, change or delete the audit trail
-- and has no access to bank data.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gateway_rw') THEN
    CREATE ROLE gateway_rw LOGIN;
  END IF;
END $$;
ALTER ROLE gateway_rw WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
  PASSWORD {{GATEWAY_RW_PASSWORD}};
ALTER ROLE gateway_rw SET statement_timeout = '5s';
GRANT USAGE ON SCHEMA app, audit TO gateway_rw;
GRANT SELECT ON app.staff_user TO gateway_rw;
GRANT INSERT ON audit.query_log TO gateway_rw;
GRANT USAGE ON SEQUENCE audit.query_log_id_seq TO gateway_rw;
GRANT INSERT ON app.feedback TO gateway_rw;
GRANT USAGE ON SEQUENCE app.feedback_id_seq TO gateway_rw;
