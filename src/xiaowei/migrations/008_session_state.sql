-- S2：可信会话状态独立于回答保存；旧请求保持 NULL，不推测旧失败原因。
ALTER TABLE xiaowei_request ADD COLUMN session_state text;
ALTER TABLE xiaowei_request ADD CONSTRAINT xiaowei_request_session_state_check
    CHECK (session_state IS NULL OR
           (state IN ('completed', 'failed') AND octet_length(session_state) <= 1024));
UPDATE xiaowei_schema_version SET version = 8;
