-- A：一次通知的独立副作用回执；不保存 URL、凭据或正文。
CREATE TABLE xiaowei_notification_attempt (
    sequence bigserial PRIMARY KEY,
    receipt_id text NOT NULL UNIQUE,
    turn_id text NOT NULL REFERENCES xiaowei_request(turn_id) ON DELETE CASCADE,
    bot_id text NOT NULL,
    bot_alias text NOT NULL,
    target_version text NOT NULL,
    content_digest text NOT NULL,
    state text NOT NULL CHECK (state IN ('sending','accepted','failed','unknown','not_sent')),
    reason text NOT NULL,
    UNIQUE (turn_id,bot_id,content_digest)
);
UPDATE xiaowei_schema_version SET version = 13;
