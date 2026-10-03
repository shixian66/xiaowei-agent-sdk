-- 小维应用表 v4：Evidence 的可信数据依赖与暂不可验证的失败码。只由 storage 的初始化或显式升级在
-- 实例锁与事务内执行。每条语句以行尾分号结束，不使用内含分号的函数体（执行时按行尾分号拆分）。

-- 声明了数据范围的工具（StarRocks）保存对象依赖（带格式版本的 JSON 文本），每次交付按当前权限
-- 与对象版本复核；其他工具为 NULL。v3 保存的 StarRocks 证据没有依赖，升级后不可读（数据范围
-- 摘要的版本同时改变），旧会话据此要求新建；不补查依赖来“修好”历史。
ALTER TABLE xiaowei_evidence ADD COLUMN dependencies text;

-- scope_unverifiable：本轮因暂时无法确认数据权限而未交付，会话与证据保留。
ALTER TABLE xiaowei_request DROP CONSTRAINT xiaowei_request_failure_code_check;
ALTER TABLE xiaowei_request ADD CONSTRAINT xiaowei_request_failure_code_check
    CHECK (
        failure_code IN (
            'busy', 'model_failed', 'evidence_failed', 'session_failed', 'scope_unverifiable',
            'result_not_saved', 'interrupted'
        )
    );

UPDATE xiaowei_schema_version SET version = 4;
