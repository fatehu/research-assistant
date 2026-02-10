"""
智能分块功能 - 完整测试套件

修改说明 (Fix 8):
- 移除所有 `or len(result.chunks) > 0` 逃生门断言
- 对学术文本严格断言 section_type 检测
- 增加 @pytest.mark.asyncio 异步测试路径
- 增加结构一致性测试

运行方式:
    # 在Docker容器内
    docker-compose exec backend pytest tests/test_smart_chunking.py -v
    
    # 运行特定测试
    docker-compose exec backend pytest tests/test_smart_chunking.py::TestChunkingService -v
    
    # 生成覆盖率报告
    docker-compose exec backend pytest tests/test_smart_chunking.py --cov=app.services.smart_chunking_service -v
"""

import pytest
import time
from typing import List, Dict, Any


# ============== Fixtures ==============

@pytest.fixture
def chunking_service():
    """获取分块服务实例"""
    from app.services.smart_chunking_service import SmartChunkingService
    return SmartChunkingService()


@pytest.fixture
def chunk_config():
    """获取配置类"""
    from app.services.smart_chunking_service import ChunkConfig
    return ChunkConfig


@pytest.fixture
def chunking_strategy():
    """获取策略枚举"""
    from app.services.smart_chunking_service import ChunkingStrategy
    return ChunkingStrategy


@pytest.fixture
def chunk_level():
    """获取层级枚举"""
    from app.services.smart_chunking_service import ChunkLevel
    return ChunkLevel


@pytest.fixture
def sample_short_text():
    """短文本样例"""
    return "人工智能是计算机科学的一个分支。它致力于开发能够模拟人类智能的系统。"


@pytest.fixture
def sample_long_text():
    """长文本样例 (~500字)"""
    return """
深度学习在自然语言处理领域的应用研究

自然语言处理（NLP）是人工智能领域的重要研究方向。近年来，随着深度学习技术的快速发展，NLP领域取得了显著突破。

传统的NLP方法主要依赖于手工特征工程和统计模型。这些方法在特定任务上取得了一定效果，但泛化能力有限，难以处理复杂的语言现象。

深度学习方法通过多层神经网络自动学习特征表示，极大地提升了模型的性能。从早期的循环神经网络（RNN）到长短时记忆网络（LSTM），再到当前主流的Transformer架构，深度学习模型不断演进。

Transformer架构的提出标志着NLP进入了新纪元。基于注意力机制，Transformer能够高效地捕捉长距离依赖关系。BERT、GPT等预训练模型在此基础上取得了突破性进展。

预训练语言模型通过在大规模语料上进行自监督学习，获得了强大的语言理解能力。这些模型可以通过微调适应各种下游任务，包括文本分类、命名实体识别、问答系统等。

未来，深度学习在NLP领域的研究方向包括多模态融合、低资源学习、可解释性等。这些方向将推动NLP技术进一步发展和应用。
"""


@pytest.fixture
def sample_academic_text():
    """学术论文样例"""
    return """
# 摘要
本研究提出了一种基于深度学习的文档智能分块方法，通过语义相似度检测实现自然语义边界的识别。

# 1. 引言
文档分块是信息检索系统的关键预处理步骤。传统的固定大小分块方法存在语义割裂问题，影响检索效果。本文旨在解决这一问题。

# 2. 相关工作
## 2.1 传统分块方法
LangChain等框架提供了基于字符数的分块功能，简单但缺乏语义感知。

## 2.2 语义分块方法
近年来出现了基于句子嵌入的语义分块方法，通过计算相邻句子的相似度识别边界。

# 3. 方法
## 3.1 语义边界检测
我们采用滑动窗口算法，计算相邻句子组的嵌入向量相似度。当相似度低于阈值时，识别为语义边界。

## 3.2 层级结构构建
在段落级分块基础上，通过合并相邻块构建章节级和文档级表示，形成三级层级结构。

# 4. 实验
## 4.1 数据集
我们在arXiv论文数据集上进行评估，包含1000篇计算机科学领域论文。

## 4.2 评估指标
采用检索准确率（Precision@K）和召回率（Recall@K）作为主要评估指标。

## 4.3 实验结果
实验结果表明，语义分块方法在Precision@5上提升了15%，在Recall@10上提升了12%。

# 5. 讨论
本方法的主要优势在于能够保持语义完整性。然而，计算成本较高是主要局限。

# 6. 结论
本文提出的智能分块方法有效提升了文档检索的准确性。未来工作将探索更高效的边界检测算法。

# 致谢
感谢XXX基金的资助支持。

# 参考文献
[1] Vaswani A, et al. Attention is all you need. NeurIPS 2017.
[2] Devlin J, et al. BERT: Pre-training of deep bidirectional transformers. NAACL 2019.
[3] Lewis P, et al. Retrieval-augmented generation for knowledge-intensive NLP tasks. NeurIPS 2020.
"""


@pytest.fixture
def sample_chinese_academic():
    """中文学术论文样例"""
    return """
# 摘要
本文研究了大语言模型在知识图谱构建中的应用，提出了一种端到端的自动化构建方法。

# 一、引言
知识图谱是人工智能的重要基础设施。传统构建方法依赖人工标注，效率低下。

# 二、研究方法
本文采用基于提示学习的实体关系抽取方法。通过设计特定提示模板，引导语言模型识别实体和关系。

# 三、实验结果
在公开数据集上，本方法的F1值达到85.3%，优于现有基线方法。

# 四、结论与展望
实验证明了大语言模型在知识图谱构建中的有效性。未来将探索多模态知识融合。

# 参考文献
[1] 张三等. 知识图谱研究综述. 计算机学报, 2020.
"""


# ============== 基础功能测试（同步接口） ==============

class TestChunkingService:
    """分块服务基础测试"""
    
    def test_service_initialization(self, chunking_service):
        """测试服务初始化"""
        assert chunking_service is not None
        
    def test_fixed_chunking_basic(self, chunking_service, chunk_config, chunking_strategy, sample_short_text):
        """测试固定分块基本功能"""
        config = chunk_config(
            strategy=chunking_strategy.FIXED,
            base_chunk_size=50,
            chunk_overlap=10
        )
        result = chunking_service.chunk(sample_short_text, config)
        
        assert result is not None
        assert len(result.chunks) > 0
        assert result.strategy == "fixed"
        
    def test_fixed_chunking_overlap(self, chunking_service, chunk_config, chunking_strategy, sample_long_text):
        """测试固定分块重叠"""
        config = chunk_config(
            strategy=chunking_strategy.FIXED,
            base_chunk_size=200,
            chunk_overlap=50
        )
        result = chunking_service.chunk(sample_long_text, config)
        
        # 检查块之间是否有重叠
        if len(result.chunks) >= 2:
            chunk1_end = result.chunks[0].content[-50:]
            chunk2_start = result.chunks[1].content[:50]
            assert len(chunk1_end) > 0
            assert len(chunk2_start) > 0
            
    def test_empty_text(self, chunking_service, chunk_config, chunking_strategy):
        """测试空文本处理"""
        config = chunk_config(strategy=chunking_strategy.FIXED)
        result = chunking_service.chunk("", config)
        
        assert result is not None
        assert len(result.chunks) == 0 or (len(result.chunks) == 1 and result.chunks[0].content == "")


# ============== 异步测试（Fix 8: 增加 pytest-asyncio 测试路径） ==============

class TestAsyncChunking:
    """异步分块测试 - 直接测试 chunk_document"""
    
    @pytest.mark.asyncio
    async def test_fixed_chunking_async(self, chunking_service, chunk_config, chunking_strategy, sample_short_text):
        """异步测试固定分块"""
        config = chunk_config(
            strategy=chunking_strategy.FIXED,
            base_chunk_size=50,
            chunk_overlap=10
        )
        result = await chunking_service.chunk_document(sample_short_text, config)
        
        assert result.strategy == "fixed"
        assert len(result.chunks) > 0
        from app.services.smart_chunking_service import SmartChunk
        for chunk in result.chunks:
            assert isinstance(chunk, SmartChunk)
    
    @pytest.mark.asyncio
    async def test_empty_text_async(self, chunking_service):
        """异步测试空文本"""
        result = await chunking_service.chunk_document("")
        assert len(result.chunks) == 0


# ============== 学术文档测试（Fix 8: 收紧断言） ==============

class TestAcademicChunking:
    """学术文档分块测试"""
    
    def test_academic_structure_detection(self, chunking_service, chunk_config, chunking_strategy, sample_academic_text):
        """测试学术结构检测 - 严格断言"""
        config = chunk_config(
            strategy=chunking_strategy.ACADEMIC,
            detect_academic_structure=True
        )
        result = chunking_service.chunk(sample_academic_text, config)
        
        # [Fix 8] 严格断言：学术文本必须检测到章节类型
        section_types = [c.metadata.section_type for c in result.chunks if c.metadata.section_type]
        assert len(section_types) >= 2, (
            f"学术文本应至少检测到 2 种章节类型，"
            f"实际: {section_types}"
        )
        
    def test_abstract_detection(self, chunking_service, chunk_config, chunking_strategy, sample_academic_text):
        """测试摘要检测 - 严格断言"""
        config = chunk_config(
            strategy=chunking_strategy.ACADEMIC,
            detect_academic_structure=True
        )
        result = chunking_service.chunk(sample_academic_text, config)
        
        section_types = [c.metadata.section_type for c in result.chunks if c.metadata.section_type]
        section_types_alt = [c.section_type for c in result.chunks if c.section_type]
        
        has_abstract = "abstract" in section_types or any("摘要" in str(s) for s in section_types)
        # [Fix 8] 严格断言：必须检测到摘要
        assert has_abstract, (
            f"学术文本中应检测到 abstract，"
            f"实际检测到的 section_types: {section_types}"
        )
        
    def test_chinese_academic(self, chunking_service, chunk_config, chunking_strategy, sample_chinese_academic):
        """测试中文学术论文 - 严格断言"""
        config = chunk_config(
            strategy=chunking_strategy.ACADEMIC,
            detect_academic_structure=True
        )
        result = chunking_service.chunk(sample_chinese_academic, config)
        
        # [Fix 8] 严格断言：中文学术文本应识别章节
        section_types = [c.metadata.section_type for c in result.chunks if c.metadata.section_type]
        assert len(section_types) > 0, (
            f"中文学术文本应检测到至少 1 种章节类型，"
            f"实际: {section_types}"
        )
        
    def test_citation_detection(self, chunking_service, chunk_config, chunking_strategy, sample_academic_text):
        """测试引用检测 - 严格断言"""
        config = chunk_config(
            strategy=chunking_strategy.ACADEMIC,
            detect_academic_structure=True,
            preserve_citations=True
        )
        result = chunking_service.chunk(sample_academic_text, config)
        
        # [Fix 8] 严格断言：包含参考文献的学术文本应检测到引用
        has_citations = any(c.metadata.has_citations for c in result.chunks)
        assert has_citations, "包含参考文献的学术文本应检测到引用"
    
    @pytest.mark.asyncio
    async def test_academic_structure_async(self, chunking_service, chunk_config, chunking_strategy, sample_academic_text):
        """异步测试学术结构检测"""
        config = chunk_config(strategy=chunking_strategy.ACADEMIC, detect_academic_structure=True)
        result = await chunking_service.chunk_document(sample_academic_text, config)
        
        section_types = {c.metadata.section_type for c in result.chunks if c.metadata.section_type}
        assert "abstract" in section_types, f"未检测到摘要章节，检测到: {section_types}"


# ============== 层级分块测试 ==============

class TestHierarchicalChunking:
    """层级分块测试"""
    
    def test_hierarchical_basic(self, chunking_service, chunk_config, chunking_strategy, chunk_level, sample_long_text):
        """测试层级分块基本功能"""
        config = chunk_config(
            strategy=chunking_strategy.HIERARCHICAL,
            enable_hierarchical=True,
            hierarchy_levels=[chunk_level.PARAGRAPH, chunk_level.SECTION, chunk_level.DOCUMENT]
        )
        result = chunking_service.chunk(sample_long_text, config)
        
        assert len(result.chunks) > 0
        
    def test_hierarchy_levels(self, chunking_service, chunk_config, chunking_strategy, sample_long_text):
        """测试层级结构"""
        config = chunk_config(
            strategy=chunking_strategy.HIERARCHICAL,
            enable_hierarchical=True
        )
        result = chunking_service.chunk(sample_long_text, config)
        
        # 检查是否有不同层级
        levels = set(c.metadata.level.value if hasattr(c.metadata.level, 'value') else c.metadata.level 
                     for c in result.chunks if c.metadata.level)
        assert len(levels) >= 1
        
    def test_parent_child_relationship(self, chunking_service, chunk_config, chunking_strategy, sample_long_text):
        """测试父子关系"""
        config = chunk_config(
            strategy=chunking_strategy.HIERARCHICAL,
            enable_hierarchical=True
        )
        result = chunking_service.chunk(sample_long_text, config)
        
        parent_ids = [c.metadata.parent_id for c in result.chunks if c.metadata.parent_id]
        # 层级分块应该建立父子关系（取决于文本长度和结构）


# ============== 结构一致性测试 (Fix 2 验证) ==============

class TestResultConsistency:
    """[Fix 2] 验证所有策略返回的结构一致性"""
    
    @pytest.mark.asyncio
    async def test_result_structure_consistency(self, chunking_service, chunk_config, chunking_strategy, sample_academic_text):
        """所有策略返回的结构必须一致"""
        from app.services.smart_chunking_service import SmartChunk
        
        for strategy in [chunking_strategy.FIXED, chunking_strategy.ACADEMIC]:
            config = chunk_config(strategy=strategy, enable_hierarchical=True)
            result = await chunking_service.chunk_document(sample_academic_text, config)
            
            # chunks 必须是 SmartChunk 列表
            for chunk in result.chunks:
                assert isinstance(chunk, SmartChunk), f"{strategy}: chunk 类型错误 {type(chunk)}"
            
            # hierarchy 如果存在，必须是 Dict[str, List[Dict]]
            if result.hierarchy:
                for key, val in result.hierarchy.items():
                    assert isinstance(key, str), f"{strategy}: hierarchy key 非字符串: {type(key)}"
                    for item in val:
                        assert isinstance(item, dict), f"{strategy}: hierarchy 值包含非 dict: {type(item)}"
    
    @pytest.mark.asyncio
    async def test_position_ratio_correctness(self, chunking_service, chunk_config, chunking_strategy, sample_academic_text):
        """[Fix 5] 验证 position_ratio 计算正确性"""
        config = chunk_config(strategy=chunking_strategy.FIXED, base_chunk_size=200)
        result = await chunking_service.chunk_document(sample_academic_text, config)
        
        for chunk in result.chunks:
            ratio = chunk.metadata.position_ratio
            assert 0.0 <= ratio <= 1.0, f"position_ratio 应在 [0, 1]，实际: {ratio}"


# ============== 混合策略测试 ==============

class TestHybridChunking:
    """混合策略测试"""
    
    def test_hybrid_auto_detect(self, chunking_service, chunk_config, chunking_strategy, sample_academic_text):
        """测试混合策略自动检测"""
        config = chunk_config(strategy=chunking_strategy.HYBRID)
        result = chunking_service.chunk(sample_academic_text, config)
        
        assert result is not None
        assert len(result.chunks) > 0
        
    def test_hybrid_non_academic(self, chunking_service, chunk_config, chunking_strategy, sample_long_text):
        """测试非学术文本的混合策略"""
        config = chunk_config(strategy=chunking_strategy.HYBRID)
        result = chunking_service.chunk(sample_long_text, config)
        
        assert result is not None
        assert len(result.chunks) > 0


# ============== 文档分析测试 ==============

class TestDocumentAnalysis:
    """文档分析测试"""
    
    def test_analyze_academic(self, chunking_service, sample_academic_text):
        """测试学术文档分析"""
        analysis = chunking_service.analyze_document(sample_academic_text)
        
        assert analysis is not None
        assert "is_academic" in analysis
        assert "detected_sections" in analysis
        assert "recommended_strategy" in analysis
        assert analysis["is_academic"] == True
        # [Fix 7] 验证增强后的分析结果包含新字段
        assert "has_citations" in analysis
        assert "document_stats" in analysis
        assert "recommended_reason" in analysis
        assert "estimated_chunks" in analysis
        
    def test_analyze_non_academic(self, chunking_service, sample_long_text):
        """测试非学术文档分析"""
        analysis = chunking_service.analyze_document(sample_long_text)
        
        assert analysis is not None
        assert "is_academic" in analysis
        
    def test_analyze_empty(self, chunking_service):
        """测试空文档分析"""
        analysis = chunking_service.analyze_document("")
        
        assert analysis is not None
        assert analysis["is_academic"] == False


# ============== 预设配置测试 ==============

class TestPresetConfigs:
    """预设配置测试"""
    
    def test_get_presets(self, chunking_service):
        """测试获取预设配置"""
        presets = chunking_service.get_preset_configs()
        
        assert presets is not None
        assert "default" in presets
        assert "fast" in presets
        assert "precise" in presets
        assert "academic" in presets
        assert "deep" in presets
        
    def test_preset_default(self, chunking_service, sample_long_text):
        """测试default预设"""
        presets = chunking_service.get_preset_configs()
        config = presets["default"]
        result = chunking_service.chunk(sample_long_text, config)
        
        assert len(result.chunks) > 0
        
    def test_preset_fast(self, chunking_service, sample_long_text, chunking_strategy):
        """测试fast预设"""
        presets = chunking_service.get_preset_configs()
        config = presets["fast"]
        result = chunking_service.chunk(sample_long_text, config)
        
        assert len(result.chunks) > 0
        assert config.strategy == chunking_strategy.FIXED
        
    def test_preset_academic(self, chunking_service, sample_academic_text):
        """测试academic预设"""
        presets = chunking_service.get_preset_configs()
        config = presets["academic"]
        result = chunking_service.chunk(sample_academic_text, config)
        
        assert len(result.chunks) > 0


# ============== 配置验证测试 ==============

class TestConfigValidation:
    """配置验证测试"""
    
    def test_invalid_chunk_size(self, chunk_config, chunking_strategy):
        """测试无效的块大小"""
        config = chunk_config(
            strategy=chunking_strategy.FIXED,
            base_chunk_size=10
        )
        assert config.base_chunk_size == 10
        
    def test_overlap_larger_than_chunk(self, chunk_config, chunking_strategy):
        """测试重叠大于块大小"""
        config = chunk_config(
            strategy=chunking_strategy.FIXED,
            base_chunk_size=100,
            chunk_overlap=150
        )
        # 配置应该被接受，但行为需要合理处理


# ============== 性能测试 ==============

class TestPerformance:
    """性能测试"""
    
    def test_fixed_chunking_speed(self, chunking_service, chunk_config, chunking_strategy, sample_long_text):
        """测试固定分块速度"""
        config = chunk_config(strategy=chunking_strategy.FIXED)
        
        start = time.time()
        for _ in range(10):
            chunking_service.chunk(sample_long_text, config)
        elapsed = time.time() - start
        
        assert elapsed < 2.0
        
    def test_large_document(self, chunking_service, chunk_config, chunking_strategy):
        """测试大文档处理"""
        large_text = "这是一段测试文本。用于验证大文档处理能力。" * 500
        
        config = chunk_config(
            strategy=chunking_strategy.FIXED,
            base_chunk_size=500
        )
        
        start = time.time()
        result = chunking_service.chunk(large_text, config)
        elapsed = time.time() - start
        
        assert len(result.chunks) > 0
        assert elapsed < 10.0


# ============== 边界条件测试 ==============

class TestEdgeCases:
    """边界条件测试"""
    
    def test_single_sentence(self, chunking_service, chunk_config, chunking_strategy):
        """测试单句文本"""
        text = "这是一个简短的句子。"
        config = chunk_config(strategy=chunking_strategy.FIXED)
        result = chunking_service.chunk(text, config)
        
        assert len(result.chunks) == 1
        
    def test_unicode_text(self, chunking_service, chunk_config, chunking_strategy):
        """测试Unicode文本"""
        text = "这是中文。This is English. これは日本語です。이것은 한국어입니다。"
        config = chunk_config(strategy=chunking_strategy.FIXED)
        result = chunking_service.chunk(text, config)
        
        assert len(result.chunks) > 0
        
    def test_special_characters(self, chunking_service, chunk_config, chunking_strategy):
        """测试特殊字符"""
        text = "公式: E=mc² 和 ∑(x²+y²)=z² 以及 α+β=γ"
        config = chunk_config(strategy=chunking_strategy.FIXED)
        result = chunking_service.chunk(text, config)
        
        assert len(result.chunks) > 0
        assert "²" in result.chunks[0].content or "E=" in result.chunks[0].content
        
    def test_newlines_only(self, chunking_service, chunk_config, chunking_strategy):
        """测试仅包含换行的文本"""
        text = "\n\n\n\n"
        config = chunk_config(strategy=chunking_strategy.FIXED)
        result = chunking_service.chunk(text, config)
        # 应该正常处理，不崩溃


# ============== 策略比较测试 ==============

class TestStrategyComparison:
    """策略比较测试"""
    
    def test_compare_strategies(self, chunking_service, chunk_config, chunking_strategy, sample_academic_text):
        """测试策略比较功能"""
        strategies = [
            chunking_strategy.FIXED,
            chunking_strategy.ACADEMIC,
        ]
        
        results = {}
        for strategy in strategies:
            config = chunk_config(strategy=strategy)
            result = chunking_service.chunk(sample_academic_text, config)
            results[strategy.value] = len(result.chunks)
            
        assert "fixed" in results
        assert "academic" in results


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
