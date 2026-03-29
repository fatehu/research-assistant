"""
语义边界检测 V2 — 独立测试套件（可离线运行）

使用 mock embedding 函数，不依赖任何外部服务（embedding API / GPU 模型）。
测试核心算法的正确性，而非 embedding 质量。

运行方式:
    pytest tests/test_semantic_boundary.py -v
    pytest tests/test_semantic_boundary.py -v --tb=short -x
"""
import pytest
import hashlib
import numpy as np
from typing import List
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


# ============== Mock Embedding ==============

def _deterministic_vector(text: str, dim: int = 128) -> List[float]:
    """基于文本内容哈希生成确定性向量（模拟 embedding）"""
    seed = int(hashlib.md5(text.encode()).hexdigest()[:8], 16) % (2**31)
    rng = np.random.RandomState(seed)
    vec = rng.randn(dim)
    # 归一化
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    return vec.tolist()


def _similar_vector(base_text: str, noise: float = 0.05, dim: int = 128) -> List[float]:
    """生成与基础文本相似的向量（加少量噪声）"""
    base = np.array(_deterministic_vector(base_text, dim))
    rng = np.random.RandomState(42)
    noisy = base + rng.randn(dim) * noise
    norm = np.linalg.norm(noisy)
    if norm > 0:
        noisy = noisy / norm
    return noisy.tolist()


async def mock_embed_fn(texts: List[str]) -> List[List[float]]:
    """Mock embedding 函数：确定性地将文本映射到向量"""
    return [_deterministic_vector(t) for t in texts]


async def mock_embed_fn_topic_groups(texts: List[str]) -> List[List[float]]:
    """
    Mock embedding 函数：模拟主题分组的效果。
    同主题的句子返回相似向量，不同主题的句子返回差异较大的向量。

    通过关键词匹配分组来模拟真实 embedding 的聚类效果。
    """
    topic_seeds = {
        "NLP": "topic_nlp_cluster",
        "Transformer": "topic_nlp_cluster",
        "注意力": "topic_nlp_cluster",
        "深度学习": "topic_nlp_cluster",
        "BERT": "topic_nlp_cluster",
        "预训练": "topic_nlp_cluster",
        "疫苗": "topic_vaccine_cluster",
        "临床": "topic_vaccine_cluster",
        "免疫": "topic_vaccine_cluster",
        "量子": "topic_quantum_cluster",
        "光子": "topic_quantum_cluster",
        "叠加": "topic_quantum_cluster",
    }

    results = []
    for text in texts:
        # 查找文本属于哪个主题
        matched_seed = None
        for keyword, seed in topic_seeds.items():
            if keyword in text:
                matched_seed = seed
                break

        if matched_seed:
            # 同主题的句子：用主题种子 + 少量文本特有噪声
            results.append(_similar_vector(matched_seed, noise=0.08))
        else:
            # 无明确主题：用文本本身做种子（可能与任何主题都不同）
            results.append(_deterministic_vector(text))

    return results


# ============== Fixtures ==============

@pytest.fixture
def chunk_config():
    from app.services.smart_chunking_service import ChunkConfig
    return ChunkConfig


@pytest.fixture
def chunking_strategy():
    from app.services.smart_chunking_service import ChunkingStrategy
    return ChunkingStrategy


@pytest.fixture
def semantic_chunker_cls():
    from app.services.smart_chunking_service import SemanticChunker
    return SemanticChunker


@pytest.fixture
def service_factory():
    from app.services.smart_chunking_service import create_chunking_service
    return create_chunking_service


# ============== 1. 边界检测核心算法测试 ==============

class TestBoundaryDetection:
    """测试 V2 相邻句子余弦距离 + 百分位断点算法"""

    @pytest.mark.asyncio
    async def test_no_boundary_for_single_topic(self, semantic_chunker_cls, chunk_config):
        """同一主题的句子之间不应产生边界"""
        config = chunk_config(breakpoint_percentile=95.0)
        chunker = semantic_chunker_cls(config, embed_fn=mock_embed_fn_topic_groups)

        sentences = [
            "深度学习在NLP中的应用越来越广泛。",
            "Transformer架构改变了NLP的研究范式。",
            "注意力机制是Transformer的核心组件。",
            "BERT模型基于Transformer实现了强大的预训练效果。",
        ]

        boundaries = await chunker.detect_semantic_boundaries(sentences)
        # 同主题句子在 P95 阈值下不应被切分
        assert len(boundaries) == 0, f"同主题句子不应产生边界，实际: {boundaries}"

    @pytest.mark.asyncio
    async def test_boundary_at_topic_shift(self, semantic_chunker_cls, chunk_config):
        """明显的主题切换处应检测到边界"""
        config = chunk_config(breakpoint_percentile=80.0)  # 降低阈值以确保检测
        chunker = semantic_chunker_cls(config, embed_fn=mock_embed_fn_topic_groups)

        sentences = [
            "深度学习在NLP中的应用越来越广泛。",
            "Transformer架构改变了NLP的研究范式。",
            "注意力机制是Transformer的核心。",
            # ← 主题跳变
            "疫苗的研发需要经过严格的临床试验。",
            "三期临床试验通常需要数万名受试者参与。",
            "疫苗的免疫效果需要长期跟踪评估。",
        ]

        boundaries = await chunker.detect_semantic_boundaries(sentences)
        # 应该在 NLP → 疫苗 的跳变处检测到边界
        assert len(boundaries) >= 1, f"应检测到至少1个主题跳变边界，实际: {boundaries}"
        # 边界应在第 3-4 句之间（index 3）
        assert any(2 <= b <= 4 for b in boundaries), (
            f"边界位置应在句子 3 附近（NLP→疫苗），实际: {boundaries}"
        )

    @pytest.mark.asyncio
    async def test_multiple_topic_shifts(self, semantic_chunker_cls, chunk_config):
        """多次主题切换应检测到多个边界"""
        config = chunk_config(breakpoint_percentile=50.0)
        chunker = semantic_chunker_cls(config, embed_fn=mock_embed_fn_topic_groups)

        sentences = [
            "深度学习在NLP中表现优异。",
            "Transformer是NLP的核心架构。",
            # ← 跳变 1
            "疫苗临床试验需要严格审批。",
            "免疫效果评估是关键步骤。",
            # ← 跳变 2
            "量子计算利用量子叠加态。",
            "光子芯片是量子计算的硬件基础。",
        ]

        boundaries = await chunker.detect_semantic_boundaries(sentences)
        assert len(boundaries) >= 2, (
            f"三个主题应产生至少2个边界，实际: {boundaries}"
        )

    @pytest.mark.asyncio
    async def test_empty_and_minimal_input(self, semantic_chunker_cls, chunk_config):
        """极端输入不应崩溃"""
        config = chunk_config()
        chunker = semantic_chunker_cls(config, embed_fn=mock_embed_fn)

        # 空列表
        assert await chunker.detect_semantic_boundaries([]) == []
        # 单句
        assert await chunker.detect_semantic_boundaries(["唯一的句子。"]) == []

    @pytest.mark.asyncio
    async def test_two_sentences(self, semantic_chunker_cls, chunk_config):
        """两个句子也应正常工作（V2 要求 >= 2 即可）"""
        config = chunk_config(breakpoint_percentile=50.0)
        chunker = semantic_chunker_cls(config, embed_fn=mock_embed_fn)

        boundaries = await chunker.detect_semantic_boundaries([
            "深度学习很重要。",
            "量子计算完全不同。",
        ])
        # 不崩溃即可，两个差异大的句子可能产生也可能不产生边界
        assert isinstance(boundaries, list)

    @pytest.mark.asyncio
    async def test_embedding_failure_returns_empty(self, semantic_chunker_cls, chunk_config):
        """Embedding 返回空时应安全降级"""
        async def bad_embed_fn(texts):
            return []

        config = chunk_config()
        chunker = semantic_chunker_cls(config, embed_fn=bad_embed_fn)

        boundaries = await chunker.detect_semantic_boundaries([
            "句子一。", "句子二。", "句子三。"
        ])
        assert boundaries == []

    @pytest.mark.asyncio
    async def test_percentile_controls_sensitivity(self, semantic_chunker_cls, chunk_config):
        """breakpoint_percentile 越低，检测到的边界越多"""
        sentences = [_deterministic_vector.__name__] * 3 + [
            f"话题{i}的内容。" for i in range(10)
        ]

        low_config = chunk_config(breakpoint_percentile=50.0)
        high_config = chunk_config(breakpoint_percentile=99.0)

        low_chunker = semantic_chunker_cls(low_config, embed_fn=mock_embed_fn)
        high_chunker = semantic_chunker_cls(high_config, embed_fn=mock_embed_fn)

        low_boundaries = await low_chunker.detect_semantic_boundaries(sentences)
        high_boundaries = await high_chunker.detect_semantic_boundaries(sentences)

        # 低百分位 → 更多边界；高百分位 → 更少边界
        assert len(low_boundaries) >= len(high_boundaries), (
            f"低阈值应产生更多(或相同)边界: "
            f"P50={len(low_boundaries)}, P99={len(high_boundaries)}"
        )


# ============== 2. 分块完整性测试 ==============

class TestChunkBySemantics:
    """测试 chunk_by_semantics 的整体行为"""

    @pytest.mark.asyncio
    async def test_chunk_preserves_original_text(self, semantic_chunker_cls, chunk_config):
        """[V2 核心] 分块结果应来自原文截取，不是空格拼接"""
        text = "第一段话。\n\n第二段话。\n\n第三段话。"
        config = chunk_config(
            breakpoint_percentile=50.0,
            min_semantic_chunk=5,
            max_semantic_chunk=500,
        )
        chunker = semantic_chunker_cls(config, embed_fn=mock_embed_fn)

        sentences = ["第一段话。", "第二段话。", "第三段话。"]
        chunks = await chunker.chunk_by_semantics(text, sentences)

        # 每个 chunk 的内容应能在原文中找到（精确子串）
        for chunk_text, start, end in chunks:
            assert text[start:end] == chunk_text or chunk_text in text, (
                f"chunk 内容应来自原文截取，实际: '{chunk_text}'"
            )

    @pytest.mark.asyncio
    async def test_chunk_no_space_join(self, semantic_chunker_cls, chunk_config):
        """[V2 核心] 中文句子间不应被插入空格"""
        text = "这是第一句。这是第二句。这是第三句。"
        config = chunk_config(
            breakpoint_percentile=99.0,
            min_semantic_chunk=5,
        )
        chunker = semantic_chunker_cls(config, embed_fn=mock_embed_fn)

        sentences = ["这是第一句。", "这是第二句。", "这是第三句。"]
        chunks = await chunker.chunk_by_semantics(text, sentences)

        for chunk_text, start, end in chunks:
            # 不应有人为添加的空格
            assert " 这是" not in chunk_text, (
                f"中文句子间不应有空格: '{chunk_text}'"
            )

    @pytest.mark.asyncio
    async def test_empty_input(self, semantic_chunker_cls, chunk_config):
        """空输入不崩溃"""
        config = chunk_config()
        chunker = semantic_chunker_cls(config, embed_fn=mock_embed_fn)

        assert await chunker.chunk_by_semantics("", []) == []
        result = await chunker.chunk_by_semantics("abc", [])
        assert len(result) == 1 and result[0][0] == "abc"


# ============== 3. 引用保护测试 ==============

class TestCitationProtection:
    """测试 V2 引用保护（分句阶段合并，非事后扩展）"""

    def test_citation_sentences_merged(self, service_factory, chunk_config):
        """以引用开头的句子应与前句合并"""
        service = service_factory()
        service._config = chunk_config(preserve_citations=True)

        text = (
            "深度学习方法表现优异。"
            "[1] Vaswani 等提出了 Transformer 架构。"
            "该方法在多项任务上取得了突破。"
        )
        sentences = service._split_to_sentences(text)

        # "[1] Vaswani ..." 应该被合并到 "深度学习方法表现优异。" 后面
        # 而不是独立成句
        citation_only = [s for s in sentences if s.strip().startswith("[1]")]
        assert len(citation_only) == 0, (
            f"以引用开头的句子应与前句合并，但仍然独立: {citation_only}"
        )

    def test_citation_protection_disabled(self, service_factory, chunk_config):
        """关闭引用保护时不合并"""
        service = service_factory()
        service._config = chunk_config(preserve_citations=False)

        text = (
            "深度学习方法表现优异。"
            "[1] Vaswani 等提出了 Transformer 架构。"
        )
        sentences = service._split_to_sentences(text)

        # 关闭保护 → 引用句保持独立
        has_citation_start = any(s.strip().startswith("[1]") for s in sentences)
        assert has_citation_start, "关闭引用保护时，引用句应保持独立"

    def test_no_uncontrolled_overlap(self, service_factory, chunk_config):
        """V2 引用保护不应产生旧版的不可控重叠"""
        service = service_factory()
        service._config = chunk_config(preserve_citations=True)

        # 每句都有引用 — 旧版会把几乎所有 chunk 都扩展导致大量重叠
        text = (
            "方法A效果好 [1]。"
            "方法B效果更好 [2]。"
            "方法C最好 [3]。"
            "总结：方法C最优 [1,2,3]。"
        )
        sentences = service._split_to_sentences(text)

        # 新版只合并以引用开头的句子，上面的句子都是引用在句末，
        # 不应触发合并，句子数应与原始分句接近
        assert len(sentences) >= 3, (
            f"句末引用不应触发合并，分句数应 >= 3，实际: {len(sentences)}"
        )


# ============== 4. 并发安全测试 ==============

class TestConcurrencySafety:
    """测试 V2 工厂函数的并发安全性"""

    def test_factory_creates_new_instances(self, service_factory):
        """每次调用工厂函数应创建独立实例"""
        s1 = service_factory()
        s2 = service_factory()
        assert s1 is not s2, "工厂函数应每次创建新实例"

    def test_instances_have_independent_state(self, service_factory):
        """不同实例的状态应互不影响"""
        s1 = service_factory()
        s2 = service_factory()

        s1._embedding_cache["test_key"] = [1.0, 2.0]
        assert "test_key" not in s2._embedding_cache, (
            "不同实例的 _embedding_cache 应独立"
        )

    def test_proxy_backward_compat(self):
        """全局 smart_chunking_service 代理应能正常调用方法"""
        from app.services.smart_chunking_service import smart_chunking_service
        # 应能调用 analyze_document 而不崩溃
        result = smart_chunking_service.analyze_document("测试文本。这是一段短文。")
        assert "is_academic" in result


# ============== 5. ChunkConfig 兼容性测试 ==============

class TestConfigBackwardCompat:
    """验证旧配置参数仍可接受"""

    def test_old_params_accepted(self, chunk_config):
        """旧的 semantic_threshold 和 window_size 参数仍应可以传入"""
        config = chunk_config(
            semantic_threshold=0.65,
            window_size=3,
            breakpoint_percentile=90.0,
        )
        assert config.semantic_threshold == 0.65
        assert config.window_size == 3
        assert config.breakpoint_percentile == 90.0

    def test_default_breakpoint_percentile(self, chunk_config):
        """默认百分位应为 95.0"""
        config = chunk_config()
        assert config.breakpoint_percentile == 95.0

    def test_presets_have_breakpoint_percentile(self):
        """所有预设配置都应有 breakpoint_percentile"""
        from app.services.smart_chunking_service import get_preset_config

        for preset in ["default", "fast", "precise", "academic", "deep"]:
            config = get_preset_config(preset)
            assert hasattr(config, "breakpoint_percentile"), (
                f"预设 '{preset}' 缺少 breakpoint_percentile"
            )
            assert 50 <= config.breakpoint_percentile <= 100


# ============== 6. 端到端分块测试（使用 mock embedding） ==============

class TestEndToEndWithMock:
    """端到端测试：使用 mock embedding 测试完整分块流程"""

    @pytest.mark.asyncio
    async def test_semantic_chunking_e2e(self, service_factory, chunk_config, chunking_strategy):
        """语义分块端到端测试"""
        service = service_factory()
        config = chunk_config(
            strategy=chunking_strategy.SEMANTIC,
            breakpoint_percentile=80.0,
            min_semantic_chunk=10,
        )

        text = """深度学习在自然语言处理领域的应用研究。

自然语言处理是人工智能的重要方向。近年来深度学习技术快速发展。

传统NLP方法依赖手工特征。这些方法泛化能力有限。

Transformer架构的提出改变了NLP研究。BERT和GPT取得了突破。

预训练模型可以微调适应各种任务。包括文本分类和问答系统。"""

        # 注入 mock embedding
        service._embedding_cache = {}
        service._embedding_call_count = 0

        with patch.object(service, '_cached_embed_texts', side_effect=mock_embed_fn):
            result = await service.chunk_document(text, config)

        assert result.strategy == "semantic"
        assert len(result.chunks) > 0
        # 验证所有 chunk 内容来自原文
        for chunk in result.chunks:
            assert chunk.content in text or text[chunk.start_char:chunk.end_char] == chunk.content

    @pytest.mark.asyncio
    async def test_fixed_chunking_no_embedding(self, service_factory, chunk_config, chunking_strategy):
        """固定分块不应调用 embedding"""
        service = service_factory()
        config = chunk_config(
            strategy=chunking_strategy.FIXED,
            base_chunk_size=100,
        )

        text = "测试文本。" * 50

        result = await service.chunk_document(text, config)
        assert result.strategy == "fixed"
        assert len(result.chunks) > 1
        assert service._embedding_call_count == 0, "固定分块不应调用 embedding"
        assert service._embedding_text_count == 0, "固定分块不应累计 embedding 文本预算"
        assert service._embedding_token_count == 0, "固定分块不应累计 embedding token 预算"

    @pytest.mark.asyncio
    async def test_embedding_budget_tracks_texts_not_calls(self, service_factory):
        """多次小批调用不应因为调用次数本身触发熔断。"""
        service = service_factory()
        service.MAX_EMBEDDING_TEXTS = 100
        service.MAX_EMBEDDING_TOKENS = 1000
        service._embedding_cache = {}
        service._embedding_call_count = 0
        service._embedding_text_count = 0
        service._embedding_token_count = 0
        service._embedding_service = SimpleNamespace(
            embed_texts=AsyncMock(side_effect=lambda texts: [[0.1, 0.2, 0.3] for _ in texts])
        )

        for idx in range(30):
            embeddings = await service._cached_embed_texts([f"sentence-{idx}"])
            assert len(embeddings) == 1

        assert service._embedding_call_count == 30
        assert service._embedding_text_count == 30
        assert service._embedding_token_count >= 30

    @pytest.mark.asyncio
    async def test_embedding_budget_exceeded_by_text_volume(self, service_factory):
        """预算应按累计文本量熔断，而不是按调用次数。"""
        from app.services.smart_chunking_service import EmbeddingLimitExceeded

        service = service_factory()
        service.MAX_EMBEDDING_TEXTS = 3
        service.MAX_EMBEDDING_TOKENS = 1000
        service._embedding_cache = {}
        service._embedding_call_count = 0
        service._embedding_text_count = 0
        service._embedding_token_count = 0
        service._embedding_service = SimpleNamespace(
            embed_texts=AsyncMock(side_effect=lambda texts: [[0.1, 0.2, 0.3] for _ in texts])
        )

        await service._cached_embed_texts(["a"])
        await service._cached_embed_texts(["b"])
        await service._cached_embed_texts(["c"])

        with pytest.raises(EmbeddingLimitExceeded) as exc_info:
            await service._cached_embed_texts(["d"])

        assert "text budget exceeded" in str(exc_info.value)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
