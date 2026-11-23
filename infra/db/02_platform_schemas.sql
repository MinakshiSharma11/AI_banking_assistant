-- Platform-owned schemas: app (staff users for mock SSO) and audit (append-only log).
-- Created IF NOT EXISTS so a reseed keeps the audit history.

CREATE SCHEMA IF NOT EXISTS app;
CREATE SCHEMA IF NOT EXISTS audit;

CREATE TABLE IF NOT EXISTS app.staff_user (
  username     VARCHAR(30) PRIMARY KEY,
  display_name VARCHAR(60) NOT NULL,
  role         VARCHAR(20) NOT NULL,     -- 'branch_manager','regional_head','head_office','it_support'
  branches     CHAR(3)[] NOT NULL        -- entitled branch codes; head_office = all
);

CREATE TABLE IF NOT EXISTS audit.query_log (
  id           BIGSERIAL PRIMARY KEY,
  ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
  username     VARCHAR(30) NOT NULL,
  question     TEXT NOT NULL,
  route        VARCHAR(12) NOT NULL,     -- 'metric','sql','api','clarify','refused','export'
  metric_query JSONB,
  sql_text     TEXT,
  row_count    INT,
  latency_ms   INT,
  llm_provider VARCHAR(10),
  llm_model    VARCHAR(40),
  status       VARCHAR(10) NOT NULL,     -- 'ok','error','blocked'
  error        TEXT,
  -- platform additions (DESIGN.md audit store): which assistant and conversation turn
  assistant       VARCHAR(12) NOT NULL DEFAULT 'data',
  conversation_id UUID,
  message_id      UUID
);

ALTER TABLE audit.query_log ADD COLUMN IF NOT EXISTS input_tokens INT;
ALTER TABLE audit.query_log ADD COLUMN IF NOT EXISTS output_tokens INT;
ALTER TABLE audit.query_log ADD COLUMN IF NOT EXISTS user_role VARCHAR(20);

-- Answer feedback (POST /feedback). A thumbs-up marks the question as a candidate
-- verified example for curators; nothing is promoted automatically.
CREATE TABLE IF NOT EXISTS app.feedback (
  id                BIGSERIAL PRIMARY KEY,
  ts                TIMESTAMPTZ NOT NULL DEFAULT now(),
  message_id        UUID NOT NULL,
  username          VARCHAR(30) NOT NULL,
  rating            SMALLINT NOT NULL CHECK (rating IN (-1, 1)),
  comment           TEXT,
  question          TEXT,
  route             VARCHAR(12),
  metric_query      JSONB,
  candidate_example BOOLEAN NOT NULL DEFAULT false
);

-- Append-only: the audit trail can be inserted into, never changed.
CREATE OR REPLACE FUNCTION audit.block_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'audit.query_log is append-only';
END $$;
DROP TRIGGER IF EXISTS query_log_append_only ON audit.query_log;
CREATE TRIGGER query_log_append_only BEFORE UPDATE OR DELETE ON audit.query_log
  FOR EACH ROW EXECUTE FUNCTION audit.block_mutation();
