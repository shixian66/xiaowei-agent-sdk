-- 小维应用表 v5：会话归属（owner）与本轮发起人（actor）分离，群请求保存受信回复目的地。只由
-- storage 的初始化或显式升级在实例锁与事务内执行。每条语句以行尾分号结束，不使用内含分号的
-- 函数体（执行时按行尾分号拆分）。
--
-- owner 是会话、历史与证据的共享归属：个人（owner_id 为内部 subject）或指定群（owner_id 由
-- 应用、租户与群标识组成）。v4 及以前的记录都是个人归属，owner_id 取原 subject_id，个人路径的
-- 请求键与摘要保持原值，升级不使个人会话、请求或证据失效。请求与证据的 subject_id 保留为本轮
-- 发起人 / 证据采集者，不被 owner 覆盖。

ALTER TABLE xiaowei_channel_session RENAME COLUMN subject_id TO owner_id;
ALTER TABLE xiaowei_channel_session
    ADD COLUMN owner_kind text NOT NULL DEFAULT 'personal'
    CHECK (owner_kind IN ('personal', 'group'));
ALTER TABLE xiaowei_channel_session ALTER COLUMN owner_kind DROP DEFAULT;
ALTER TABLE xiaowei_channel_session DROP CONSTRAINT xiaowei_channel_session_pkey;
ALTER TABLE xiaowei_channel_session
    ADD PRIMARY KEY (channel, owner_kind, owner_id, conversation_key, generation);
DROP INDEX xiaowei_channel_session_current_idx;
CREATE UNIQUE INDEX xiaowei_channel_session_current_idx
    ON xiaowei_channel_session (channel, owner_kind, owner_id, conversation_key)
    WHERE state = 'current';

ALTER TABLE xiaowei_session RENAME COLUMN subject_id TO owner_id;
ALTER TABLE xiaowei_session
    ADD COLUMN owner_kind text NOT NULL DEFAULT 'personal'
    CHECK (owner_kind IN ('personal', 'group'));
ALTER TABLE xiaowei_session ALTER COLUMN owner_kind DROP DEFAULT;

ALTER TABLE xiaowei_evidence
    ADD COLUMN owner_kind text NOT NULL DEFAULT 'personal'
    CHECK (owner_kind IN ('personal', 'group'));
ALTER TABLE xiaowei_evidence ALTER COLUMN owner_kind DROP DEFAULT;
ALTER TABLE xiaowei_evidence ADD COLUMN owner_id text;
UPDATE xiaowei_evidence SET owner_id = subject_id;
ALTER TABLE xiaowei_evidence ALTER COLUMN owner_id SET NOT NULL;
DROP INDEX xiaowei_evidence_owner_idx;
CREATE INDEX xiaowei_evidence_owner_idx ON xiaowei_evidence (owner_kind, owner_id, session_id);

-- 群请求的回复目的地取自已验证的入站事件（群与原消息），之后的发送与重发只能发往这里；个人
-- 请求不保存目的地。access_denied：群请求在开始运行前未能确认发起人当前的成员资格或使用权限。
ALTER TABLE xiaowei_request
    ADD COLUMN owner_kind text NOT NULL DEFAULT 'personal'
    CHECK (owner_kind IN ('personal', 'group'));
ALTER TABLE xiaowei_request ALTER COLUMN owner_kind DROP DEFAULT;
ALTER TABLE xiaowei_request ADD COLUMN owner_id text;
UPDATE xiaowei_request SET owner_id = subject_id;
ALTER TABLE xiaowei_request ALTER COLUMN owner_id SET NOT NULL;
ALTER TABLE xiaowei_request ADD COLUMN reply_chat_id text;
ALTER TABLE xiaowei_request ADD COLUMN reply_message_id text;
ALTER TABLE xiaowei_request ADD CONSTRAINT xiaowei_request_reply_target_check
    CHECK (
        (owner_kind = 'group') = (reply_chat_id IS NOT NULL)
        AND (owner_kind = 'group') = (reply_message_id IS NOT NULL)
    );
ALTER TABLE xiaowei_request DROP CONSTRAINT xiaowei_request_failure_code_check;
ALTER TABLE xiaowei_request ADD CONSTRAINT xiaowei_request_failure_code_check
    CHECK (
        failure_code IN (
            'busy', 'model_failed', 'evidence_failed', 'session_failed', 'scope_unverifiable',
            'access_denied', 'result_not_saved', 'interrupted'
        )
    );

UPDATE xiaowei_schema_version SET version = 5;
