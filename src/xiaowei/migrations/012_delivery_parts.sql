-- D1a：新请求使用分项回执；旧请求保留原发送语义。
ALTER TABLE xiaowei_request ADD COLUMN delivery_version smallint NOT NULL DEFAULT 0
    CHECK (delivery_version IN (0, 1));
CREATE TABLE xiaowei_delivery_part (
    turn_id text NOT NULL REFERENCES xiaowei_request(turn_id) ON DELETE CASCADE,
    sequence integer NOT NULL CHECK (sequence >= 0),
    kind text NOT NULL CHECK (kind IN ('approval', 'body', 'md', 'html')),
    body bytea NOT NULL CHECK (octet_length(body) > 0),
    file_key text CHECK (file_key <> ''),
    upload_state text NOT NULL DEFAULT 'pending'
        CHECK (upload_state IN ('pending', 'uploading', 'uploaded', 'unknown')),
    send_state text NOT NULL DEFAULT 'pending'
        CHECK (send_state IN ('pending', 'sending', 'sent', 'failed', 'unknown')),
    attempt text,
    uuid text NOT NULL CHECK (uuid <> ''),
    message_id text CHECK (message_id <> ''),
    PRIMARY KEY (turn_id, sequence),
    CHECK ((upload_state = 'uploaded') = (file_key IS NOT NULL)),
    CHECK ((send_state = 'sent') = (message_id IS NOT NULL)),
    CHECK (kind NOT IN ('md', 'html') OR octet_length(body) <= 400000)
);
UPDATE xiaowei_schema_version SET version = 12;
