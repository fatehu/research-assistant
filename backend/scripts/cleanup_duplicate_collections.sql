-- 清理重复的收藏夹
-- 运行方式: docker exec -i research_postgres psql -U yui -d research_assistant < backend/scripts/cleanup_duplicate_collections.sql

-- 显示重复的收藏夹
SELECT user_id, name, COUNT(*) as count 
FROM paper_collections 
GROUP BY user_id, name 
HAVING COUNT(*) > 1;

-- 删除重复收藏夹中的关联（保留每组中ID最小的）
DELETE FROM paper_collection 
WHERE collection_id IN (
    SELECT id FROM paper_collections p1
    WHERE EXISTS (
        SELECT 1 FROM paper_collections p2
        WHERE p1.user_id = p2.user_id 
        AND p1.name = p2.name 
        AND p1.id > p2.id
    )
);

-- 删除重复的收藏夹本身
DELETE FROM paper_collections 
WHERE id IN (
    SELECT id FROM paper_collections p1
    WHERE EXISTS (
        SELECT 1 FROM paper_collections p2
        WHERE p1.user_id = p2.user_id 
        AND p1.name = p2.name 
        AND p1.id > p2.id
    )
);

-- 验证清理结果
SELECT user_id, name, COUNT(*) as count 
FROM paper_collections 
GROUP BY user_id, name 
HAVING COUNT(*) > 1;
