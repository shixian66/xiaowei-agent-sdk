-- H2：有界调查结果与原数据期限；旧运行保持空快照，不修改SDK表。
ALTER TABLE xiaowei_scheduled_run ADD COLUMN result_snapshot text;
ALTER TABLE xiaowei_scheduled_run ADD COLUMN result_expires_at timestamptz;
UPDATE xiaowei_schema_version SET version=16;
