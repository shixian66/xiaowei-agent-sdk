ALTER TABLE xiaowei_session ADD COLUMN ds_clusters JSONB;
UPDATE xiaowei_schema_version SET version=17;
