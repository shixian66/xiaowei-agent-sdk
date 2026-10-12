-- SDK-A：仅保存 SDK 续跑检查点与可信原轮账本；审批/执行事实仍属于 Action。
ALTER TABLE xiaowei_action ADD COLUMN failure_code text;
ALTER TABLE xiaowei_action ADD CONSTRAINT xiaowei_action_failure_check CHECK (
    failure_code IS NULL OR (state = 'rejected' AND failure_code IN (
        'action_requester_invalid', 'action_session_invalid', 'action_upgrade_invalidated',
        'action_expired', 'action_rejected', 'action_unresumable'
    ))
);
-- v8没有RunState；只作废旧pending，其他执行事实（包括executing）不改。
UPDATE xiaowei_action SET state = 'rejected', failure_code = 'action_upgrade_invalidated'
WHERE state = 'pending';

CREATE TABLE xiaowei_action_run (
    turn_id text PRIMARY KEY,
    owner_id text NOT NULL,
    context text NOT NULL,
    run_state text NOT NULL,
    ledger text NOT NULL,
    message text NOT NULL,
    binding text NOT NULL,
    scope_checks integer NOT NULL CHECK(scope_checks >= 0),
    version integer NOT NULL CHECK(version > 0),
    consumed boolean NOT NULL,
    material_turn_id text NOT NULL,
    expires_at timestamptz NOT NULL
);
CREATE INDEX xiaowei_action_run_expiry_idx ON xiaowei_action_run(expires_at);

ALTER TABLE xiaowei_request DROP CONSTRAINT xiaowei_request_state_check;
ALTER TABLE xiaowei_request ADD CONSTRAINT xiaowei_request_state_check
    CHECK(state IN ('accepted', 'running', 'paused', 'completed', 'failed', 'interrupted'));
-- 前八个迁移实测的原answer/state约束；保持简单SQL迁移，不改变拆分器。
ALTER TABLE xiaowei_request DROP CONSTRAINT xiaowei_request_check;
ALTER TABLE xiaowei_request ADD CONSTRAINT xiaowei_request_answer_state_check
    CHECK((state IN ('completed', 'paused')) = (answer IS NOT NULL));
ALTER TABLE xiaowei_request DROP CONSTRAINT xiaowei_request_failure_code_check;
ALTER TABLE xiaowei_request ADD CONSTRAINT xiaowei_request_failure_code_check CHECK (
    failure_code IN ('busy','model_failed','evidence_failed','session_failed','scope_unverifiable',
        'access_denied','result_not_saved','interrupted','action_not_approvable',
        'action_feedback_failed','action_unknown','action_rejected','action_requester_invalid',
        'action_session_invalid','action_upgrade_invalidated','action_expired',
        'action_unresumable')
);
UPDATE xiaowei_schema_version SET version = 9;
