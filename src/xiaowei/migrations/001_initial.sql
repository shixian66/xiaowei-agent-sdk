-- 小维应用表 v1。只由 storage.initialize_storage 在一个事务内执行；SDK Session 表由 SDK 管理，
-- 这里不创建、不修改。Evidence 只保存按用途投影后的内容，不保存原始工具结果。
-- 每条语句以行尾分号结束，不使用内含分号的函数体（执行时按行尾分号拆分）。

CREATE TABLE xiaowei_schema_version (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    version integer NOT NULL
);

CREATE TABLE xiaowei_evidence (
    evidence_id text PRIMARY KEY,
    subject_id text NOT NULL,
    session_id text NOT NULL,
    turn_id text NOT NULL,
    channel text NOT NULL CHECK (channel IN ('web', 'feishu')),
    target_id text NOT NULL,
    tool_id text NOT NULL,
    call_id text NOT NULL,
    policy_id text NOT NULL,
    policy_fingerprint text NOT NULL,
    captured_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    truncated boolean NOT NULL,
    model_content text NOT NULL,
    session_content text NOT NULL,
    web_content text NOT NULL,
    feishu_content text NOT NULL
);

CREATE INDEX xiaowei_evidence_owner_idx ON xiaowei_evidence (subject_id, session_id);

INSERT INTO xiaowei_schema_version (version) VALUES (1);
