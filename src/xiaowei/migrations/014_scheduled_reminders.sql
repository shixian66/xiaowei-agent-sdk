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
-- 已过提醒时间的批准是确定拒绝；渠道只展示这个固定安全类别，不透传异常。
ALTER TABLE xiaowei_action DROP CONSTRAINT xiaowei_action_failure_check;
ALTER TABLE xiaowei_action ADD CONSTRAINT xiaowei_action_failure_check CHECK (
    failure_code IS NULL OR (state = 'rejected' AND failure_code IN (
        'action_requester_invalid', 'action_session_invalid', 'action_upgrade_invalidated',
        'action_expired', 'action_rejected', 'action_unresumable', 'reminder_time_passed'
    ))
);
ALTER TABLE xiaowei_request DROP CONSTRAINT xiaowei_request_failure_code_check;
ALTER TABLE xiaowei_request ADD CONSTRAINT xiaowei_request_failure_code_check CHECK (
    failure_code IN ('busy','model_failed','evidence_failed','session_failed','scope_unverifiable',
        'access_denied','result_not_saved','interrupted','action_not_approvable',
        'action_feedback_failed','action_unknown','action_rejected','action_requester_invalid',
        'action_session_invalid','action_upgrade_invalidated','action_expired',
        'action_unresumable','reminder_time_passed')
);
UPDATE xiaowei_schema_version SET version = 14;
