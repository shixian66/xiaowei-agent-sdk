-- E6：报告原文件及上传阶段与请求绑定；清理请求时级联删除原文件。
CREATE TABLE xiaowei_report (
    turn_id text PRIMARY KEY REFERENCES xiaowei_request(turn_id) ON DELETE CASCADE,
    format text NOT NULL CHECK (format IN ('md', 'html')),
    body bytea NOT NULL CHECK (octet_length(body) BETWEEN 1 AND 400000),
    digest text NOT NULL CHECK (octet_length(digest) = 64),
    upload_state text NOT NULL CHECK (upload_state IN ('pending', 'uploading', 'uploaded', 'unknown')),
    file_key text CHECK (file_key <> ''),
    expires_at timestamptz NOT NULL,
    CHECK ((upload_state = 'uploaded') = (file_key IS NOT NULL))
);
UPDATE xiaowei_schema_version SET version = 11;
