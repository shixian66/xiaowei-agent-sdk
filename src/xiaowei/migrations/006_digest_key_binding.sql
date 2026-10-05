-- 小维应用表 v6：把数据库与摘要密钥绑定。只保存固定标签的 HMAC-SHA256 指纹，不保存密钥。
-- 全新初始化在本迁移后、同一事务内写入首个绑定；已有 v1-v5 只由显式升级确认后绑定。
-- 每条语句以行尾分号结束，不使用内含分号的函数体（执行时按行尾分号拆分）。

CREATE TABLE xiaowei_installation (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    format_version integer NOT NULL CHECK (format_version = 1),
    digest_key_fingerprint text NOT NULL
        CHECK (digest_key_fingerprint ~ '^[0-9a-f]{64}$')
);

UPDATE xiaowei_schema_version SET version = 6;
