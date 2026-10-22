-- C：固定提醒不变；Agent 每个计划时点独立运行、独立发送机会。
ALTER TABLE xiaowei_scheduled_job ADD COLUMN kind text NOT NULL DEFAULT 'fixed_message'
    CHECK (kind IN ('fixed_message','agent_task'));
ALTER TABLE xiaowei_scheduled_job ADD COLUMN agent_spec text;
ALTER TABLE xiaowei_scheduled_job ALTER COLUMN content_expires_at DROP NOT NULL;
ALTER TABLE xiaowei_scheduled_job ADD COLUMN last_long_notice_at timestamptz;
ALTER TABLE xiaowei_scheduled_job ADD COLUMN notice_id text;
ALTER TABLE xiaowei_scheduled_job ADD COLUMN notice_reason text;
ALTER TABLE xiaowei_scheduled_job ADD COLUMN notice_state text
    CHECK(notice_state IN ('pending','sending','sent','failed','unknown'));
ALTER TABLE xiaowei_scheduled_job ADD COLUMN next_run timestamptz;
UPDATE xiaowei_scheduled_job SET next_run=run_at;
CREATE TABLE xiaowei_scheduled_run (
    run_id text PRIMARY KEY,
    job_id text NOT NULL REFERENCES xiaowei_scheduled_job(job_id),
    scheduled_at timestamptz NOT NULL,
    session_id text NOT NULL,
    turn_id text NOT NULL,
    state text NOT NULL CHECK (state IN ('queued','running','completed','failed','unknown','skipped')),
    reason text NOT NULL,
    queued_at timestamptz NOT NULL,
    started_at timestamptz,
    finished_at timestamptz,
    UNIQUE(job_id,scheduled_at)
);
ALTER TABLE xiaowei_notification_attempt ADD COLUMN run_id text UNIQUE REFERENCES xiaowei_scheduled_run(run_id);
ALTER TABLE xiaowei_notification_attempt DROP CONSTRAINT notification_origin;
ALTER TABLE xiaowei_notification_attempt ADD CONSTRAINT notification_origin
    CHECK (num_nonnulls(turn_id,job_id,run_id)=1);
UPDATE xiaowei_schema_version SET version=15;
