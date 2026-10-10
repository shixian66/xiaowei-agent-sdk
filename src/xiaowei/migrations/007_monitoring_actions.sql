-- W5：仅监控动作的应用事实；不修改 SDK 表，不保存凭据或原始远端结果。
-- 无 Session 外键：会话关闭/清理不能删除仍有效的动作及执行事实。
CREATE TABLE xiaowei_action (
    action_id text PRIMARY KEY,
    owner_id text NOT NULL,
    requester_id text NOT NULL,
    proposal_session_id text NOT NULL,
    proposal_turn_id text NOT NULL,
    call_id text NOT NULL,
    proposal_tool_id text NOT NULL,
    write_tool_id text NOT NULL,
    target_id text NOT NULL,
    binding text NOT NULL,
    plan text NOT NULL,
    state text NOT NULL CHECK (state IN ('pending', 'executing', 'succeeded', 'rejected', 'unknown')),
    attempt text,
    approver_id text,
    approval_turn_id text,
    result text,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    approval_expires_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    UNIQUE (owner_id, proposal_turn_id, call_id),
    CHECK (approval_expires_at <= expires_at),
    CHECK (state NOT IN ('executing', 'succeeded', 'unknown') OR attempt IS NOT NULL),
    CHECK (state <> 'pending' OR attempt IS NULL),
    CHECK ((attempt IS NOT NULL) = (approver_id IS NOT NULL)),
    CHECK ((attempt IS NOT NULL) = (approval_turn_id IS NOT NULL))
);

CREATE INDEX xiaowei_action_retention_idx ON xiaowei_action (expires_at);

-- 批准回执按已保存的动作事实分类；只保存固定类别，不保存异常文字或动作参数。
ALTER TABLE xiaowei_request DROP CONSTRAINT xiaowei_request_failure_code_check;
ALTER TABLE xiaowei_request ADD CONSTRAINT xiaowei_request_failure_code_check
    CHECK (
        failure_code IN (
            'busy', 'model_failed', 'evidence_failed', 'session_failed', 'scope_unverifiable',
            'access_denied', 'result_not_saved', 'interrupted',
            'action_not_approvable', 'action_feedback_failed', 'action_unknown', 'action_rejected'
        )
    );

UPDATE xiaowei_schema_version SET version = 7;
