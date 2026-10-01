-- 小维应用表 v3：投递尝试归属。只由 storage 的初始化或显式升级在实例锁与事务内执行。
-- 每条语句以行尾分号结束，不使用内含分号的函数体（执行时按行尾分号拆分）。

-- 每次取得投递权写入一个新的随机尝试标识，发送结束只能落定同一标识的尝试；结束或启动恢复
-- 时清空。delivery_owner_* 只由不持实例锁的显式重发命令写入：它在发送期间占用的数据库连接
-- 身份，启动恢复据此跳过仍在进行的重发。v2 遗留的 sending 没有尝试标识，由启动恢复记为 unknown。
ALTER TABLE xiaowei_request ADD COLUMN delivery_attempt text;
ALTER TABLE xiaowei_request ADD COLUMN delivery_owner_pid integer;
ALTER TABLE xiaowei_request ADD COLUMN delivery_owner_started timestamptz;
ALTER TABLE xiaowei_request ADD CONSTRAINT xiaowei_request_attempt_only_while_sending
    CHECK (delivery_attempt IS NULL OR delivery = 'sending');
ALTER TABLE xiaowei_request ADD CONSTRAINT xiaowei_request_owner_needs_attempt
    CHECK (
        (delivery_owner_pid IS NULL) = (delivery_owner_started IS NULL)
        AND (delivery_owner_pid IS NULL OR delivery_attempt IS NOT NULL)
    );

UPDATE xiaowei_schema_version SET version = 3;
