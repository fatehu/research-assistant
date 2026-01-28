-- 清理重复的收藏夹
-- 运行方式: docker exec -i research_postgres psql -U yui -d research_assistant
-- 然后粘贴以下 SQL

-- 1. 显示重复的收藏夹
SELECT user_id, name, COUNT(*) as count 
FROM paper_collections 
GROUP BY user_id, name 
HAVING COUNT(*) > 1;

-- 2. 删除重复收藏夹中的论文关联（保留每组中ID最小的）
DELETE FROM paper_collection 
WHERE collection_id IN (
    SELECT p1.id FROM paper_collections p1
    WHERE EXISTS (
        SELECT 1 FROM paper_collections p2
        WHERE p1.user_id = p2.user_id 
        AND p1.name = p2.name 
        AND p1.id > p2.id
    )
);

-- 3. 删除重复的收藏夹本身
DELETE FROM paper_collections 
WHERE id IN (
    SELECT p1.id FROM paper_collections p1
    WHERE EXISTS (
        SELECT 1 FROM paper_collections p2
        WHERE p1.user_id = p2.user_id 
        AND p1.name = p2.name 
        AND p1.id > p2.id
    )
);

-- 4. 验证清理结果
SELECT user_id, name, COUNT(*) as count 
FROM paper_collections 
GROUP BY user_id, name 
HAVING COUNT(*) > 1;
