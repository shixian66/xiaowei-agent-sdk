ALTER TABLE xiaowei_session ADD COLUMN ds_clusters JSONB;
ALTER TABLE xiaowei_session ADD COLUMN ds_selection_turn TEXT;
UPDATE xiaowei_schema_version SET version=17;
