-- 小维应用表 v2：渠道会话映射与请求结果。全新初始化在 001 之后于同一事务执行；已有 v1 只由
-- 显式 storage.upgrade_storage 在实例锁与事务内执行。只建立 xiaowei_ 表，不修改 SDK 表。
-- 不保存原消息、原始查询结果、原始异常或凭据：会话语境与请求编号只保存带服务端密钥的摘要。
-- 每条语句以行尾分号结束，不使用内含分号的函数体（执行时按行尾分号拆分）。

-- 渠道会话映射：每个 (channel, subject, conversation) 同时只有一个 current；新建会话递增 generation。
CREATE TABLE xiaowei_channel_session (
    channel text NOT NULL CHECK (channel IN ('web', 'feishu')),
    subject_id text NOT NULL,
    conversation_key text NOT NULL,
    generation integer NOT NULL CHECK (generation >= 1),
    session_id text NOT NULL UNIQUE,
    state text NOT NULL CHECK (state IN ('current', 'retired')),
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (channel, subject_id, conversation_key, generation)
);

CREATE UNIQUE INDEX xiaowei_channel_session_current_idx
    ON xiaowei_channel_session (channel, subject_id, conversation_key) WHERE state = 'current';

-- 请求与可重发结果：answer 只在 completed 时保存受限 AgentAnswer JSON；failure_code 只取安全
-- 失败码闭集（与 channel_store.FailureCode 一致），interrupted 只属于 interrupted 状态。
CREATE TABLE xiaowei_request (
    channel text NOT NULL CHECK (channel IN ('web', 'feishu')),
    request_key text NOT NULL,
    subject_id text NOT NULL,
    conversation_key text NOT NULL,
    session_id text NOT NULL,
    turn_id text NOT NULL UNIQUE,
    mode text NOT NULL CHECK (mode IN ('query', 'diagnose')),
    message_digest text NOT NULL,
    state text NOT NULL
        CHECK (state IN ('accepted', 'running', 'completed', 'failed', 'interrupted')),
    answer text,
    failure_code text CHECK (
        failure_code IN (
            'busy', 'model_failed', 'evidence_failed', 'session_failed', 'result_not_saved',
            'interrupted'
        )
    ),
    delivery text NOT NULL CHECK (delivery IN ('pending', 'sending', 'sent', 'failed', 'unknown')),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    PRIMARY KEY (channel, request_key),
    CHECK ((state = 'completed') = (answer IS NOT NULL)),
    CHECK ((state IN ('failed', 'interrupted')) = (failure_code IS NOT NULL)),
    CHECK ((state = 'interrupted') = (failure_code = 'interrupted'))
);

CREATE INDEX xiaowei_request_session_idx ON xiaowei_request (session_id);

UPDATE xiaowei_schema_version SET version = 2;
