-- 修改 shared_resources.resource_id 列类型
-- 从 INTEGER 改为 VARCHAR(50) 以支持 Notebook 的 UUID
-- 
-- 使用方法：
-- docker exec -i research_postgres psql -U research_user -d research_assistant < scripts/migrate_resource_id_type.sql

-- 检查当前列类型
SELECT column_name, data_type 
FROM information_schema.columns 
WHERE table_name = 'shared_resources' AND column_name = 'resource_id';

-- 修改列类型（将整数转换为字符串）
ALTER TABLE shared_resources 
ALTER COLUMN resource_id TYPE VARCHAR(50) USING resource_id::VARCHAR;

-- 验证修改
SELECT column_name, data_type 
FROM information_schema.columns 
WHERE table_name = 'shared_resources' AND column_name = 'resource_id';

-- 完成提示
SELECT '列类型修改完成！resource_id 现在是 VARCHAR(50) 类型' AS status;
