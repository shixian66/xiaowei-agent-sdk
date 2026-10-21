-- B：一个不可变定义对应一个一次性 occurrence；不保存后台 Agent 状态。
ALTER TABLE xiaowei_session ADD COLUMN task_content_purged boolean NOT NULL DEFAULT false;
CREATE TABLE xiaowei_scheduled_job (
    job_id text PRIMARY KEY,
    owner_id text NOT NULL,
    creator_id text NOT NULL,
    bot_id text NOT NULL,
    bot_alias text NOT NULL,
    target_version text NOT NULL,
    run_at timestamptz NOT NULL,
    lateness_seconds integer NOT NULL CHECK (lateness_seconds > 0),
    content_expires_at timestamptz NOT NULL,
    text text,
    definition_evidence_ids text NOT NULL,
    source_context text NOT NULL,
    state text NOT NULL CHECK (state IN ('draft','active','paused','cancelled','completed','missed','disabled')),
    approved_at timestamptz,
    approval_id text,
    approver_id text,
    reason text NOT NULL DEFAULT 'pending',
    unverified_count integer NOT NULL DEFAULT 0,
    checked_at timestamptz,
    last_success_at timestamptz
);
CREATE INDEX xiaowei_scheduled_due ON xiaowei_scheduled_job (run_at) WHERE state='active';
ALTER TABLE xiaowei_notification_attempt ALTER COLUMN turn_id DROP NOT NULL;
ALTER TABLE xiaowei_notification_attempt ADD COLUMN job_id text UNIQUE REFERENCES xiaowei_scheduled_job(job_id);
-- 状态查询的风险提示只确认这一条未知回执，不能误确认后来出现的未知结果。
ALTER TABLE xiaowei_notification_attempt ADD COLUMN warning_sequence bigint;
ALTER TABLE xiaowei_notification_attempt ADD CONSTRAINT notification_origin CHECK ((turn_id IS NULL) <> (job_id IS NULL));
UPDATE xiaowei_schema_version SET version = 14;
