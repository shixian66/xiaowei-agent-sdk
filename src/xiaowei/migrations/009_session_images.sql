-- E5b：当前飞书会话原图。应用表独立于 SDK Session；原值不进入 Evidence 或历史 JSON。
CREATE TABLE xiaowei_image (
    image_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    session_id text NOT NULL REFERENCES xiaowei_channel_session(session_id) ON DELETE CASCADE,
    turn_id text NOT NULL REFERENCES xiaowei_request(turn_id) ON DELETE CASCADE,
    source_digest text NOT NULL CHECK (octet_length(source_digest) <= 128),
    content_digest text NOT NULL CHECK (octet_length(content_digest) = 64),
    mime_type text NOT NULL CHECK (mime_type IN ('image/png', 'image/jpeg')),
    original bytea NOT NULL CHECK (octet_length(original) BETWEEN 1 AND 2097152),
    expires_at timestamptz NOT NULL,
    UNIQUE (session_id, turn_id),
    UNIQUE (session_id, source_digest)
);
CREATE INDEX xiaowei_image_expiry_idx ON xiaowei_image (expires_at);
UPDATE xiaowei_schema_version SET version = 9;
