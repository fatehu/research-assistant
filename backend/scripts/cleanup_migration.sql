-- 多角色系统迁移清理脚本
-- 用于清理失败迁移后残留的数据
-- 
-- 在 Docker 中运行:
-- docker exec -i research_postgres psql -U research_user -d research_assistant < cleanup_migration.sql

-- 删除表（如果存在）
DROP TABLE IF EXISTS announcement_reads CASCADE;
DROP TABLE IF EXISTS announcements CASCADE;
DROP TABLE IF EXISTS shared_resources CASCADE;
DROP TABLE IF EXISTS invitations CASCADE;
DROP TABLE IF EXISTS group_members CASCADE;
DROP TABLE IF EXISTS research_groups CASCADE;

-- 删除用户表的角色相关列（如果存在）
ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_users_role;
ALTER TABLE users DROP CONSTRAINT IF EXISTS fk_users_mentor_id;
DROP INDEX IF EXISTS ix_users_mentor_id;
DROP INDEX IF EXISTS ix_users_role;
ALTER TABLE users DROP COLUMN IF EXISTS joined_at;
ALTER TABLE users DROP COLUMN IF EXISTS research_direction;
ALTER TABLE users DROP COLUMN IF EXISTS department;
ALTER TABLE users DROP COLUMN IF EXISTS mentor_id;
ALTER TABLE users DROP COLUMN IF EXISTS role;

-- 删除枚举类型（如果存在）
DROP TYPE IF EXISTS share_permission CASCADE;
DROP TYPE IF EXISTS share_type CASCADE;
DROP TYPE IF EXISTS invitation_status CASCADE;
DROP TYPE IF EXISTS user_role CASCADE;

-- 清除迁移记录
DELETE FROM alembic_version WHERE version_num = '006_multi_role';

-- 输出完成信息
SELECT '清理完成! 现在可以重新启动容器运行迁移了。' AS status;
