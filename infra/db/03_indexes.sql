-- Created after bulk load (faster COPY). Supports the RLS EXISTS checks and metric filters.
CREATE INDEX ON core.customer (home_branch);
CREATE INDEX ON core.cust_account (branch_code);
CREATE INDEX ON core.cust_account (customer_no);
CREATE INDEX ON core.cust_account (account_open_date);
CREATE INDEX ON core.transaction (account_no);
CREATE INDEX ON core.transaction (txn_date);
CREATE INDEX ON obdx.digx_user (customer_no);
CREATE INDEX ON obdx.digx_user_session (user_id);
CREATE INDEX ON obdx.digx_user_session (login_at);
CREATE INDEX ON obdx.origination_application (account_no);
CREATE INDEX ON obdx.origination_application (customer_no);
CREATE INDEX ON atum.wf_instance (branch_code);
CREATE INDEX ON atum.wf_instance (wf_code, status);
CREATE INDEX ON atum.wf_stage_history (instance_id);
CREATE INDEX ON atum.wf_stage_history (exited_at);
CREATE INDEX ON atum.wf_document (instance_id);
ANALYZE;
