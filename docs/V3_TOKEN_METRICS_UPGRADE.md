# V3 Token 计量统一升级文档

## 变更概要

**问题**: `base_chunk_size` 等所有尺寸参数基于**字符数**，而非 Token 数。
- 500 个英文字符 ≈ 125 Tokens
- 500 个中文字符 ≈ 333 Tokens
- **同样的配置下，中文块包含的信息量是英文的 2.5~4 倍**，可能导致:
  - Embedding 模型输入超出最大 Token 限制
  - 检索精度下降（块过大，语义稀释）
  - 中英文混合文档分块质量不一致

**解决方案**: 新增 Token 计量模式 (`use_token_based=True`)
- 用户配置以 Token 为单位（128 Tokens、384 Tokens 等）
- 运行时根据实际文本的中/英文比例，自动换算为字符限制
- 英文文档: 128 Tokens → ~512 字符
- 中文文档: 128 Tokens → ~192 字符
- 中英混合: 按实际比例加权

**关于 breakpoint_percentile 滑块**: 审查后确认逻辑无问题。
- 值从前端 slider(20~99) → schema → `ChunkConfig.breakpoint_percentile` → `np.percentile(dist_array, percentile)`
- 含义正确: 值越高 → 阈值越高 → 切分越少 → 块越大
- 边界距离过近时有去重保护 (`boundary_pos - boundaries[-1]) >= 2`)

---

## 修改的文件清单

### 新增文件

| 文件路径 | 说明 |
|---------|------|
| `backend/app/services/smart_chunking/token_utils.py` | **核心**: Token 估算、字符↔Token 互转、语言检测、自适应限制计算 |

### 修改文件

| 文件路径 | 变更内容 |
|---------|---------|
| `backend/app/services/smart_chunking/types.py` | `ChunkConfig` 新增 Token 字段 + `resolve_char_limits()` 方法; 新增 `ResolvedCharLimits` 数据类; `ChunkMetadata` 新增 `token_count` |
| `backend/app/services/smart_chunking/semantic_chunker.py` | 接收 `ResolvedCharLimits`, 所有尺寸比较使用 `self.limits` 而非直接读 config |
| `backend/app/services/smart_chunking/service.py` | 主入口调用 `config.resolve_char_limits(text)`, 注入到 SemanticChunker; stats 新增 token 统计; 预设配置新增 Token 字段 |
| `backend/app/services/smart_chunking/__init__.py` | 导出新增的类型和函数 |
| `backend/app/schemas/chunking.py` | `ChunkingConfigCreate` 新增 Token 字段; `ChunkingStatsResponse` 新增 token 统计; `ChunkMetadataResponse` 新增 `token_count` |
| `backend/app/api/chunking.py` | `_convert_to_chunk_config` 传递 Token 字段; 响应包含 Token 统计 |
| `backend/app/services/document_service.py` | `estimate_tokens` 委托给 `token_utils`（更精确） |

### 需要手动修改的文件 (本包未包含，因为文件过大)

#### 1. `frontend/src/services/api.ts`

在类型定义区域，找到 `ChunkingConfig` 接口，新增以下字段:

```typescript
export interface ChunkingConfig {
  strategy: ChunkingStrategy
  // V3 Token 计量新增
  use_token_based: boolean
  base_chunk_tokens: number
  overlap_tokens: number
  min_semantic_tokens: number
  max_semantic_tokens: number
  // 字符计量（旧）
  base_chunk_size: number
  chunk_overlap: number
  // ... 其余字段不变
}
```

`ChunkMetadata` 新增:
```typescript
token_count?: number
```

`ChunkingStats` 新增:
```typescript
total_tokens?: number
avg_chunk_tokens?: number
min_chunk_tokens?: number
max_chunk_tokens?: number
```

`DocumentAnalysis.document_stats` 新增:
```typescript
total_tokens?: number
```

完整的替换内容请参考 `frontend/src/services/api_types_patch.ts`。

#### 2. `frontend/src/pages/knowledge/SmartChunkingPage.tsx`

自定义配置 state 新增 Token 字段:
```typescript
const [customConfig, setCustomConfig] = useState<Partial<ChunkingConfig>>({
  // ... 原有字段
  use_token_based: true,         // 新增
  base_chunk_tokens: 128,        // 新增
  overlap_tokens: 16,            // 新增
  min_semantic_tokens: 32,       // 新增
  max_semantic_tokens: 384,      // 新增
})
```

在 "基础分块参数" 区域添加 Token/字符切换开关和对应的 Token 滑块:
```tsx
{/* Token/字符计量模式切换 */}
<Form.Item label={<Text>计量模式</Text>}>
  <Switch
    checked={customConfig.use_token_based}
    onChange={(v) => setCustomConfig({ ...customConfig, use_token_based: v })}
    checkedChildren="Token"
    unCheckedChildren="字符"
  />
  <Text className="text-slate-500 text-xs ml-2">
    {customConfig.use_token_based
      ? 'Token 模式: 自动适配中英文信息密度（推荐）'
      : '字符模式: 按字符数切分（旧行为）'}
  </Text>
</Form.Item>

{customConfig.use_token_based ? (
  // Token 模式的滑块组
  <>
    <Slider min={32} max={512} step={16}
      value={customConfig.base_chunk_tokens}
      onChange={(v) => setCustomConfig({...customConfig, base_chunk_tokens: v})}
      marks={{ 32: '32', 128: '128', 256: '256', 512: '512' }}
    />
    {/* ... overlap_tokens, min/max_semantic_tokens 类似 */}
  </>
) : (
  // 字符模式的滑块组 (原有代码)
  <Slider min={100} max={2000} ... />
)}
```

在分块结果显示区域, 可展示 Token 统计:
```tsx
<Tag color="blue">平均 {testResult.stats.avg_chunk_tokens || '?'} Token/块</Tag>
```

ChunkCard 组件新增 Token 显示:
```tsx
<div className="mt-2 text-slate-500 text-xs">
  {chunk.content.length} 字符 | {chunk.metadata.token_count || '?'} Tokens
</div>
```

#### 3. `backend/app/api/knowledge.py`

在 `process_document_task` 函数中（约 641 行），构建 `ChunkConfig` 时新增 Token 字段:

```python
chunk_config = ChunkConfig(
    strategy=ChunkingStrategy(kb_config.get("strategy", "hybrid")),
    use_token_based=kb_config.get("use_token_based", True),         # 新增
    base_chunk_tokens=kb_config.get("base_chunk_tokens", 128),       # 新增
    overlap_tokens=kb_config.get("overlap_tokens", 16),              # 新增
    min_semantic_tokens=kb_config.get("min_semantic_tokens", 32),    # 新增
    max_semantic_tokens=kb_config.get("max_semantic_tokens", 384),   # 新增
    base_chunk_size=kb.chunk_size,
    chunk_overlap=kb.chunk_overlap,
    # ... 其余不变
)
```

### 无需删除的文件

本次升级是**增量修改**，不需要删除任何文件。

---

## Token 比率说明（如实声明）

本方案使用**经验估算值**来换算 Token 和字符:

| 语言 | 字符/Token 比率 | 来源/说明 |
|------|---------------|----------|
| 英文 | ~4.0 | 基于 GPT/BERT 系 tokenizer 的经验值（含空格、标点） |
| 中文 | ~1.5 | 一个汉字通常拆为 1~2 tokens，加标点平均约 1.5 |
| 混合 | 加权平均 | 按实际文本的 CJK / non-CJK 字符比例动态计算 |

**已知局限性**:
1. 不同的 embedding 模型使用不同的 tokenizer（bge-m3 vs text-embedding-v2 vs cl100k_base），实际比率可能存在 **±20%** 偏差
2. 对于日文、韩文等其他 CJK 语言，使用了相同的中文比率，精度可能更低
3. 对于代码、公式等特殊内容，比率偏差可能更大

**但与旧方案（纯字符计数）相比**:
- 旧方案的中英文偏差高达 **400%**（4~5倍）
- 新方案在最差情况下偏差约 **20~30%**
- 这已经是一个**数量级的改善**

如果需要更精确的 Token 计数，未来可以引入实际 tokenizer（如 `tiktoken`），但这会增加依赖和计算开销。对于分块尺寸控制这个场景，当前精度已经足够。

---

## 向后兼容性

| 场景 | 行为 |
|------|------|
| 已有知识库（metadata 中无 `use_token_based` 字段） | 默认 `use_token_based=True`，首次使用 Token 模式 |
| 前端发送旧格式请求（无 Token 字段） | Pydantic 默认值生效，自动使用 Token 模式 |
| 显式 `use_token_based=False` | 回退到纯字符模式，行为与 V2 完全一致 |
| 旧的字符字段 (`base_chunk_size` 等) | 仍保留在 schema 和 config 中，`use_token_based=False` 时仍有效 |
| `semantic_threshold` / `window_size` | 仍标记为 [已弃用]，保持不变 |

---

## 数据库迁移

本次变更**不需要数据库迁移**。所有新增的 Token 配置字段都存储在 `knowledge_bases.metadata` JSON 字段中。

---

## 测试建议

1. **单元测试**: 对 `token_utils.py` 中的函数编写测试:
   - `estimate_tokens("Hello world")` → 约 3
   - `estimate_tokens("你好世界")` → 约 3
   - `compute_adaptive_char_limits(128, "Hello world...")` → base_chunk_chars ≈ 512
   - `compute_adaptive_char_limits(128, "这是一段中文...")` → base_chunk_chars ≈ 192

2. **集成测试**: 分别用纯中文、纯英文、混合文本调用 `POST /api/chunking/preview`
   - 验证中文文档的 avg_chunk_tokens 和英文文档接近（都约 128）
   - 验证中文文档的 avg_chunk_size（字符）明显小于英文

3. **前端测试**: 在 SmartChunkingPage 切换 Token/字符模式，验证滑块和预览结果
