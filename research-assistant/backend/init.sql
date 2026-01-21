-- 启用 pgvector 向量扩展
CREATE EXTENSION IF NOT EXISTS vector;

-- 验证扩展已安装
SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';
